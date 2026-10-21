"""Durable, auditable two-phase write records for the X starter.

The MCP server prepares an X API write, the caller confirms it with a phrase,
and only then does the server perform the HTTP call. Keeping that record in a
process-local dictionary loses the confirmation across a restart and cannot
tell "never started" apart from "started, outcome unknown", so a crash could
either drop a confirmed write or publish twice.

This module stores the records in one JSON file with a hash-chained audit log.
It never persists post text, confirmation phrases, or credentials, and it uses
only the Python standard library.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "STATES",
    "TERMINAL",
    "WriteStore",
    "WriteStoreError",
    "AUDIT_MAX_EVENTS",
    "GENESIS_HASH",
    "SCHEMA_VERSION",
]

STATES: tuple[str, ...] = (
    "prepared",
    "executing",
    "completed",
    "failed",
    "uncertain",
    "expired",
)
TERMINAL: frozenset[str] = frozenset({"completed", "failed", "uncertain", "expired"})

SCHEMA_VERSION = 1
AUDIT_MAX_EVENTS = 5000
GENESIS_HASH = "0" * 64

_FORBIDDEN_KEY_PARTS = ("token", "secret", "password", "authorization")
_WRITE_ATTEMPTS = 10
_WRITE_RETRY_SLEEP = 0.05


def _now_iso(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _parse_iso(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _audit_body(event: Mapping[str, Any]) -> dict[str, Any]:
    """The hashed part of an audit event: everything except prev and hash."""

    return {key: value for key, value in event.items() if key not in {"hash", "prev"}}


def _forbidden_key(value: Any, trail: str = "") -> str | None:
    """Return the first key name that looks like a credential, else None."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            lowered = name.lower()
            for part in _FORBIDDEN_KEY_PARTS:
                if part in lowered:
                    return f"{trail}{name}"
            found = _forbidden_key(item, f"{trail}{name}.")
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = _forbidden_key(item, f"{trail}{index}.")
            if found is not None:
                return found
    return None


class WriteStoreError(Exception):
    """A store error that is safe to expose through an MCP tool result."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class WriteStore:
    """Thread-safe, optionally persistent store for two-phase X API writes."""

    def __init__(
        self,
        path: str | os.PathLike | None,
        *,
        max_records: int = 1000,
        retention_seconds: int = 7 * 24 * 3600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path: Path | None = Path(path) if path is not None else None
        self._max_records = max(1, int(max_records))
        self._retention_seconds = max(0, int(retention_seconds))
        self._clock = clock
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self._audit: list[dict[str, Any]] = []
        self._order: dict[str, int] = {}
        self._order_counter = 0
        self._recovered_from_corruption = 0
        with self._lock:
            self._load()

    # ------------------------------------------------------------------ load

    def _load(self) -> None:
        if self._path is None:
            return
        if not self._path.exists():
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise WriteStoreError(
                "WRITE_STORE_CORRUPT",
                "The write store file cannot be read.",
                details={"path": str(self._path), "reason": type(exc).__name__},
            ) from None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if not self._shape_ok(parsed):
            self._quarantine_corrupt_file()
            return
        assert isinstance(parsed, dict)  # narrowed by _shape_ok
        records: dict[str, dict[str, Any]] = {}
        for key, value in parsed["records"].items():
            if isinstance(value, dict):
                records[str(key)] = value
        audit = [event for event in parsed["audit"] if isinstance(event, dict)]
        if len(audit) > AUDIT_MAX_EVENTS:
            audit = audit[-AUDIT_MAX_EVENTS:]
        self._records = records
        self._audit = audit
        self._rebuild_order()

        now = float(self._clock())
        changed = self._recover_interrupted(now)
        pruned = self._pruned(self._records, now)
        if len(pruned) != len(self._records):
            changed = True
        if changed:
            self._records = pruned
            self._rebuild_order()
            self._persist()

    @staticmethod
    def _shape_ok(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        if value.get("version") != SCHEMA_VERSION:
            return False
        if not isinstance(value.get("records"), dict):
            return False
        return isinstance(value.get("audit"), list)

    def _quarantine_corrupt_file(self) -> None:
        """Move an unreadable store aside; start empty and say so in stats()."""

        assert self._path is not None
        target = self._path.with_name(
            f"{self._path.name}.corrupt-{int(float(self._clock()))}"
        )
        suffix = 1
        while target.exists():
            target = self._path.with_name(
                f"{self._path.name}.corrupt-{int(float(self._clock()))}-{suffix}"
            )
            suffix += 1
        try:
            os.replace(self._path, target)
        except OSError as exc:
            raise WriteStoreError(
                "WRITE_STORE_CORRUPT",
                "The write store file is corrupt and cannot be moved aside.",
                details={"path": str(self._path), "reason": type(exc).__name__},
            ) from None
        self._recovered_from_corruption = 1

    def _rebuild_order(self) -> None:
        order: dict[str, int] = {}
        counter = 0
        for event in self._audit:
            write_id = event.get("write_id")
            if isinstance(write_id, str) and write_id not in order:
                order[write_id] = counter
                counter += 1
        for write_id in self._records:
            if write_id not in order:
                order[write_id] = counter
                counter += 1
        self._order = order
        self._order_counter = counter

    def _recover_interrupted(self, now: float) -> bool:
        """An "executing" record may or may not have reached X. Mark it unknown."""

        changed = False
        for write_id, record in list(self._records.items()):
            if record.get("state") != "executing":
                continue
            updated = copy.deepcopy(record)
            updated["state"] = "uncertain"
            updated["recovery_reason"] = "process_restarted"
            updated["updated_at"] = _now_iso(now)
            self._records[write_id] = updated
            self._audit = self._append_audit(
                self._audit,
                write_id,
                "executing",
                "uncertain",
                {"reason": "process_restarted", "text_sha256": updated.get("text_sha256")},
            )
            changed = True
        return changed

    def _pruned(self, records: Mapping[str, dict[str, Any]], now: float) -> dict[str, dict[str, Any]]:
        kept: dict[str, dict[str, Any]] = {}
        for write_id, record in records.items():
            if record.get("state") in TERMINAL:
                updated = _parse_iso(record.get("updated_at"))
                if updated is not None and updated + self._retention_seconds <= now:
                    continue
            kept[write_id] = record
        return kept

    # ------------------------------------------------------------- audit log

    def _append_audit(
        self,
        audit: list[dict[str, Any]],
        write_id: str,
        from_state: str | None,
        to_state: str,
        detail: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        previous = audit[-1].get("hash") if audit else GENESIS_HASH
        if not isinstance(previous, str):
            previous = GENESIS_HASH
        sequence = 1
        if audit:
            last = audit[-1].get("seq")
            if isinstance(last, int) and not isinstance(last, bool):
                sequence = last + 1
            else:
                sequence = len(audit) + 1
        event: dict[str, Any] = {
            "seq": sequence,
            "at": _now_iso(float(self._clock())),
            "write_id": write_id,
            "from": from_state,
            "to": to_state,
        }
        if detail:
            # Only states and the text hash are audited; never the post text,
            # the confirmation phrase, or any credential.
            text_sha256 = detail.get("text_sha256")
            if isinstance(text_sha256, str) and text_sha256:
                event["text_sha256"] = text_sha256
            reason = detail.get("reason")
            if isinstance(reason, str) and reason:
                event["reason"] = reason
        event["prev"] = previous
        # hash = sha256(prev + canonical_json(event without "prev" and "hash"))
        event["hash"] = hashlib.sha256(
            previous.encode("utf-8") + _canonical_bytes(_audit_body(event))
        ).hexdigest()
        tail = list(audit)
        tail.append(event)
        if len(tail) > AUDIT_MAX_EVENTS:
            tail = tail[-AUDIT_MAX_EVENTS:]
        return tail

    @staticmethod
    def _chain_ok(events: list[dict[str, Any]]) -> bool:
        """Verify the retained chain, starting at its first kept event."""

        for index, event in enumerate(events):
            if not isinstance(event, dict):
                return False
            previous = event.get("prev")
            stored = event.get("hash")
            if not isinstance(previous, str) or not isinstance(stored, str):
                return False
            if index > 0 and previous != events[index - 1].get("hash"):
                return False
            expected = hashlib.sha256(
                previous.encode("utf-8") + _canonical_bytes(_audit_body(event))
            ).hexdigest()
            if expected != stored:
                return False
        return True

    def audit(self, *, limit: int = 50) -> dict[str, Any]:
        with self._lock:
            events = self._audit
            tail = events[-limit:] if isinstance(limit, int) and limit > 0 else []
            return {
                "events": copy.deepcopy(tail),
                "chain_ok": self._chain_ok(events),
                "head": events[-1]["hash"] if events else "",
            }

    # ------------------------------------------------------------- persistence

    def _payload(self, records: Mapping[str, Any], audit: list[dict[str, Any]]) -> str:
        try:
            return json.dumps(
                {"version": SCHEMA_VERSION, "records": records, "audit": audit},
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Write records and audit events must be JSON serializable."
            ) from exc

    def _commit(
        self, records: dict[str, dict[str, Any]], audit: list[dict[str, Any]]
    ) -> None:
        """Write the new state, then publish it in memory only if it landed."""

        if self._path is None:
            self._records = records
            self._audit = audit
            return
        text = self._payload(records, audit)
        self._write_text(text)
        self._records = records
        self._audit = audit

    def _persist(self) -> None:
        if self._path is None:
            return
        self._write_text(self._payload(self._records, self._audit))

    def _write_text(self, text: str) -> None:
        path = self._path
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        payload = text.encode("utf-8")
        try:
            with temporary.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            # Windows can briefly deny replacement while a reader or scanner
            # holds the destination. The temporary file is complete, so a
            # short retry is safe and keeps the rename atomic when it succeeds.
            for attempt in range(_WRITE_ATTEMPTS):
                try:
                    os.replace(temporary, path)
                    return
                except OSError as exc:
                    transient = getattr(exc, "winerror", None) in {5, 32, 33}
                    transient = transient or isinstance(exc, PermissionError)
                    if not transient or attempt == _WRITE_ATTEMPTS - 1:
                        raise
                    time.sleep(_WRITE_RETRY_SLEEP)
        except OSError as exc:
            raise WriteStoreError(
                "WRITE_STORE_CORRUPT",
                "The write store file cannot be written.",
                details={"path": str(path), "reason": type(exc).__name__},
            ) from None
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    # --------------------------------------------------------------- records

    def create(self, record: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(record, Mapping):
            raise ValueError("record must be a mapping.")
        forbidden = _forbidden_key(record)
        if forbidden is not None:
            raise ValueError(f"record field {forbidden!r} looks like a credential.")
        write_id = record.get("write_id")
        if not isinstance(write_id, str) or not write_id.strip():
            raise ValueError("record must contain a non-empty string write_id.")
        with self._lock:
            existing = self._records.get(write_id)
            if existing is not None:
                raise WriteStoreError(
                    "WRITE_STATE_CONFLICT",
                    "The write record already exists.",
                    details={
                        "write_id": write_id,
                        "state": existing.get("state"),
                        "reason": "duplicate_write_id",
                    },
                )
            now = float(self._clock())
            stamp = _now_iso(now)
            candidate = copy.deepcopy(dict(record))
            candidate["write_id"] = write_id
            candidate["state"] = "prepared"
            candidate["created_at"] = stamp
            candidate["updated_at"] = stamp
            records = dict(self._records)
            if len(records) >= self._max_records:
                records = self._pruned(records, now)
                if len(records) >= self._max_records:
                    raise WriteStoreError(
                        "WRITE_STORE_FULL",
                        "The write store reached its record limit.",
                        details={"max_records": self._max_records},
                    )
            records[write_id] = candidate
            audit = self._append_audit(
                self._audit,
                write_id,
                None,
                "prepared",
                {"text_sha256": candidate.get("text_sha256")},
            )
            self._commit(records, audit)
            self._order[write_id] = self._order_counter
            self._order_counter += 1
            return copy.deepcopy(candidate)

    def get(self, write_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(write_id)
            return copy.deepcopy(record) if record is not None else None

    def transition(
        self,
        write_id: str,
        expected: str | Iterable[str],
        new_state: str,
        **fields: Any,
    ) -> dict[str, Any]:
        if new_state not in STATES:
            raise ValueError(f"unknown write state {new_state!r}.")
        if fields:
            forbidden = _forbidden_key(fields)
            if forbidden is not None:
                raise ValueError(f"field {forbidden!r} looks like a credential.")
        if isinstance(expected, str):
            wanted = {expected}
        else:
            wanted = {str(state) for state in expected}
        with self._lock:
            record = self._records.get(write_id)
            if record is None:
                raise WriteStoreError(
                    "WRITE_NOT_FOUND",
                    "The write record does not exist.",
                    details={"write_id": write_id},
                )
            current = record.get("state")
            if current not in wanted:
                raise WriteStoreError(
                    "WRITE_STATE_CONFLICT",
                    "The write record is not in an expected state.",
                    details={
                        "write_id": write_id,
                        "state": current,
                        "expected": sorted(wanted),
                    },
                )
            updated = copy.deepcopy(record)
            for key, value in fields.items():
                updated[key] = copy.deepcopy(value)
            updated["write_id"] = write_id
            updated["state"] = new_state
            updated["updated_at"] = _now_iso(float(self._clock()))
            records = dict(self._records)
            records[write_id] = updated
            audit = self._append_audit(
                self._audit,
                write_id,
                current,
                new_state,
                {
                    "text_sha256": updated.get("text_sha256"),
                    "reason": fields.get("reason"),
                },
            )
            self._commit(records, audit)
            return copy.deepcopy(updated)

    def expire_due(self, now: float | None = None) -> int:
        with self._lock:
            moment = float(self._clock() if now is None else now)
            due: list[str] = []
            for write_id, record in self._records.items():
                if record.get("state") != "prepared":
                    continue
                expires = record.get("expires_epoch")
                if isinstance(expires, bool) or not isinstance(expires, (int, float)):
                    continue
                if float(expires) < moment:
                    due.append(write_id)
            if not due:
                return 0
            records = dict(self._records)
            audit = self._audit
            for write_id in due:
                updated = copy.deepcopy(self._records[write_id])
                updated["state"] = "expired"
                updated["updated_at"] = _now_iso(moment)
                records[write_id] = updated
                audit = self._append_audit(
                    audit,
                    write_id,
                    "prepared",
                    "expired",
                    {"reason": "confirmation_expired", "text_sha256": updated.get("text_sha256")},
                )
            self._commit(records, audit)
            return len(due)

    def list(self, *, session_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            if not isinstance(limit, int) or limit <= 0:
                return []
            selected = [
                record
                for record in self._records.values()
                if session_id is None or record.get("session_id") == session_id
            ]
            selected.sort(
                key=lambda record: (
                    str(record.get("created_at") or ""),
                    self._order.get(str(record.get("write_id")), 0),
                ),
                reverse=True,
            )
            return [copy.deepcopy(record) for record in selected[:limit]]

    def stats(self) -> dict[str, int]:
        with self._lock:
            counts: dict[str, int] = {state: 0 for state in STATES}
            for record in self._records.values():
                state = record.get("state")
                if isinstance(state, str) and state in counts:
                    counts[state] += 1
                else:
                    counts["unknown"] = counts.get("unknown", 0) + 1
            result: dict[str, int] = dict(counts)
            result["total"] = len(self._records)
            if self._recovered_from_corruption:
                result["recovered_from_corruption"] = self._recovered_from_corruption
            return result
