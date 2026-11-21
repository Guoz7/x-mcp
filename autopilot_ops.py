"""Bounded autopilot authorization, micro-wave scheduling, and 7-day ops summary.

Instead of asking the owner to type a confirmation phrase for every 3-5 action
wave, the owner authorizes one bounded window (duration, active local hours,
per-wave cap, and daily goals). Each wave still runs through the circuit
breaker, single-use action leases, and page postcondition verification.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from daily_action_budget import LOCAL_TIMEZONE, _local_date, _parse_epoch
from ledger_lock import LedgerLock


class AutopilotError(Exception):
    """Structured error safe to return through MCP and the web UI."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class AutopilotStore:
    """Durable JSON store for bounded autopilot windows and wave execution logs."""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "autopilot.json"
        self.lock = LedgerLock(self.state_dir / ".autopilot.lock")

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "prepared": {}, "active_window": None, "waves_log": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("prepared", {})
                data.setdefault("active_window", None)
                data.setdefault("waves_log", [])
                return data
        except ValueError:
            pass
        return {"version": 1, "prepared": {}, "active_window": None, "waves_log": []}

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def prepare_window(
        self,
        *,
        days: int = 7,
        active_hours: tuple[int, int] | list[int] = (9, 22),
        max_per_wave: int = 4,
        min_wave_interval_minutes: int = 90,
        daily_goals: dict[str, int] | None = None,
        persona_topics: list[str] | None = None,
        exclude_keywords: list[str] | None = None,
        search_query: str | None = None,
        now_epoch: float | None = None,
    ) -> dict[str, Any]:
        now = float(now_epoch) if now_epoch is not None else time.time()
        if not 1 <= int(days) <= 30:
            raise AutopilotError("INVALID_ARGUMENT", "days must be between 1 and 30.")
        if not isinstance(active_hours, (list, tuple)) or len(active_hours) != 2:
            raise AutopilotError("INVALID_ARGUMENT", "active_hours must be [start_hour, end_hour].")
        start_h, end_h = int(active_hours[0]), int(active_hours[1])
        if not (0 <= start_h < end_h <= 24):
            raise AutopilotError("INVALID_ARGUMENT", "active_hours must satisfy 0 <= start < end <= 24.")
        if not 1 <= int(max_per_wave) <= 10:
            raise AutopilotError("INVALID_ARGUMENT", "max_per_wave must be between 1 and 10.")
        if not 15 <= int(min_wave_interval_minutes) <= 720:
            raise AutopilotError("INVALID_ARGUMENT", "min_wave_interval_minutes must be between 15 and 720.")

        goals = daily_goals or {
            "publish_post": 1,
            "like_post": 12,
            "follow_profile": 6,
            "repost_post": 3,
            "reply_post": 2,
        }
        window_id = "ap_" + secrets.token_hex(6)
        phrase = "AUTOPILOT " + secrets.token_hex(3).upper()
        record = {
            "window_id": window_id,
            "confirmation_phrase": phrase,
            "days": int(days),
            "active_hours": [start_h, end_h],
            "max_per_wave": int(max_per_wave),
            "min_wave_interval_minutes": int(min_wave_interval_minutes),
            "daily_goals": goals,
            "persona_topics": [str(k).strip() for k in (persona_topics or []) if str(k).strip()][:20],
            "exclude_keywords": [str(k).strip() for k in (exclude_keywords or []) if str(k).strip()][:20],
            "search_query": str(search_query).strip()[:120] if search_query else None,
            "created_at": _iso(now),
            "expires_epoch": now + 600,
        }
        with self.lock:
            data = self._read()
            data["prepared"][window_id] = record
            self._write(data)
        return {
            "window_id": window_id,
            "confirmation_phrase": phrase,
            "days": int(days),
            "active_hours": [start_h, end_h],
            "max_per_wave": int(max_per_wave),
            "min_wave_interval_minutes": int(min_wave_interval_minutes),
            "daily_goals": goals,
            "expires_at": _iso(record["expires_epoch"]),
        }

    def activate_window(
        self,
        window_id: str,
        confirmation: str,
        *,
        now_epoch: float | None = None,
    ) -> dict[str, Any]:
        now = float(now_epoch) if now_epoch is not None else time.time()
        with self.lock:
            data = self._read()
            prep = data["prepared"].get(window_id)
            if not isinstance(prep, dict):
                raise AutopilotError("WINDOW_NOT_FOUND", "The prepared autopilot window does not exist.")
            if now > float(prep["expires_epoch"]):
                raise AutopilotError("WINDOW_EXPIRED", "The autopilot confirmation expired.")
            if confirmation != prep["confirmation_phrase"]:
                raise AutopilotError("CONFIRMATION_MISMATCH", "The confirmation phrase does not match.")

            end_epoch = now + int(prep["days"]) * 86400
            active = {
                "window_id": window_id,
                "enabled": True,
                "activated_at": _iso(now),
                "expires_at": _iso(end_epoch),
                "expires_epoch": end_epoch,
                "active_hours": prep["active_hours"],
                "max_per_wave": prep["max_per_wave"],
                "min_wave_interval_minutes": prep["min_wave_interval_minutes"],
                "daily_goals": prep["daily_goals"],
                "persona_topics": prep.get("persona_topics") or [],
                "exclude_keywords": prep.get("exclude_keywords") or [],
                "search_query": prep.get("search_query"),
                "last_wave_epoch": None,
                "stop_reason": None,
            }
            data["active_window"] = active
            data["prepared"].pop(window_id, None)
            self._write(data)
            return active

    def stop_window(self, *, reason: str = "manual_stop", now_epoch: float | None = None) -> dict[str, Any]:
        now = float(now_epoch) if now_epoch is not None else time.time()
        with self.lock:
            data = self._read()
            active = data.get("active_window")
            if isinstance(active, dict):
                active["enabled"] = False
                active["stopped_at"] = _iso(now)
                active["stop_reason"] = reason
                self._write(data)
                return dict(active)
            return {"enabled": False, "stop_reason": reason}

    def record_wave(self, summary: dict[str, Any], *, now_epoch: float | None = None) -> None:
        now = float(now_epoch) if now_epoch is not None else time.time()
        with self.lock:
            data = self._read()
            active = data.get("active_window")
            if isinstance(active, dict):
                active["last_wave_epoch"] = now
            log = list(data.get("waves_log") or [])
            log.append({"at": _iso(now), "at_epoch": now, **summary})
            data["waves_log"] = log[-50:]
            self._write(data)

    def status(self, *, now_epoch: float | None = None) -> dict[str, Any]:
        now = float(now_epoch) if now_epoch is not None else time.time()
        with self.lock:
            data = self._read()
            active = data.get("active_window")
            waves_log = list(data.get("waves_log") or [])
        if not isinstance(active, dict) or not active.get("enabled"):
            return {
                "enabled": False,
                "active_window": active,
                "in_active_hours": False,
                "wave_due": False,
                "recent_waves": waves_log[-10:],
            }
        if now >= float(active.get("expires_epoch", 0)):
            return {
                "enabled": False,
                "active_window": {**active, "enabled": False, "stop_reason": "window_expired"},
                "in_active_hours": False,
                "wave_due": False,
                "recent_waves": waves_log[-10:],
            }
        local_hour = datetime.fromtimestamp(now, LOCAL_TIMEZONE).hour
        start_h, end_h = active["active_hours"]
        in_hours = start_h <= local_hour < end_h
        last_wave = active.get("last_wave_epoch")
        min_interval = int(active.get("min_wave_interval_minutes", 90)) * 60
        interval_ok = last_wave is None or (now - float(last_wave)) >= min_interval
        return {
            "enabled": True,
            "active_window": active,
            "in_active_hours": in_hours,
            "wave_due": in_hours and interval_ok,
            "next_wave_epoch": (float(last_wave) + min_interval) if last_wave is not None else now,
            "recent_waves": waves_log[-10:],
        }


def build_ops_summary(
    ledger: dict[str, Any],
    autopilot_status: dict[str, Any],
    *,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Summarize 7-day activity, follow-back rate, uncertain rate, and recent waves."""
    now = float(now_epoch) if now_epoch is not None else time.time()
    today = _local_date(now)
    days = [(today - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
    daily: dict[str, dict[str, int]] = {
        d: {"publish_post": 0, "like_post": 0, "follow_profile": 0, "repost_post": 0, "reply_post": 0}
        for d in days
    }
    totals = {"completed": 0, "uncertain": 0, "failed": 0}
    follows_verified = 0
    follows_mutual = 0
    recent_actions: list[tuple[float, dict[str, Any]]] = []
    latest_snapshot: tuple[float, dict[str, Any]] | None = None
    _WRITE_TYPES = {"publish_post", "like_post", "repost_post", "reply_post", "follow_profile", "unfollow_profile"}

    actions = ledger.get("actions")
    for action in (actions.values() if isinstance(actions, dict) else ()):
        if not isinstance(action, dict):
            continue
        status = action.get("status")
        at_epoch = _parse_epoch(action.get("completed_at")) or _parse_epoch(action.get("created_at"))
        if at_epoch is None:
            continue
        atype = str(action.get("type") or "")
        result = action.get("result") or {}
        payload = action.get("payload") or {}
        if atype == "owner_read_account" and status == "completed" and isinstance(result, dict):
            snap = result.get("snapshot") if isinstance(result.get("snapshot"), dict) else result
            if snap.get("username") and (latest_snapshot is None or at_epoch > latest_snapshot[0]):
                latest_snapshot = (at_epoch, snap)
        if atype in _WRITE_TYPES:
            target_url = (
                result.get("reply_url")
                or result.get("post_url")
                or result.get("profile_url")
                or payload.get("post_url")
                or payload.get("profile_url")
                or payload.get("url")
                or ""
            )
            handle = (
                result.get("handle")
                or result.get("author")
                or payload.get("handle")
                or payload.get("author")
                or ""
            )
            recent_actions.append((
                at_epoch,
                {
                    "action_id": action.get("action_id"),
                    "type": atype,
                    "status": status,
                    "verified": bool(result.get("verified") is True),
                    "handle": str(handle),
                    "url": str(target_url),
                    "text": str(result.get("text") or payload.get("text") or "")[:120],
                    "at": action.get("completed_at") or action.get("created_at"),
                },
            ))
        day_str = _local_date(at_epoch).isoformat()
        if day_str not in daily:
            continue
        if status in totals:
            totals[status] += 1
        if status != "completed":
            continue
        if result.get("verified") is not True:
            continue
        if atype in daily[day_str]:
            daily[day_str][atype] += 1
        if atype == "follow_profile":
            follows_verified += 1
            if result.get("mutual_verified") is True:
                follows_mutual += 1

    total_attempts = totals["completed"] + totals["uncertain"] + totals["failed"]
    uncertain_rate = round(totals["uncertain"] / total_attempts, 3) if total_attempts else 0.0
    follow_back_rate = round(follows_mutual / follows_verified, 3) if follows_verified else 0.0
    recent_actions.sort(key=lambda pair: pair[0], reverse=True)

    return {
        "window_days": 7,
        "timezone": "Asia/Shanghai",
        "account_binding": ledger.get("account_binding"),
        "last_account_snapshot": latest_snapshot[1] if latest_snapshot else None,
        "totals": totals,
        "uncertain_rate": uncertain_rate,
        "follow_funnel": {
            "verified_follows": follows_verified,
            "mutual_verified": follows_mutual,
            "follow_back_rate": follow_back_rate,
        },
        "daily_breakdown": [{"date": d, **daily[d]} for d in days],
        "recent_actions": [item for _, item in recent_actions[:30]],
        "autopilot": autopilot_status,
    }


class AutopilotScheduler:
    """Lightweight background daemon thread that runs due autopilot waves."""

    def __init__(self, tick_fn: Any, *, poll_seconds: float = 60.0) -> None:
        import threading

        self._tick_fn = tick_fn
        self._poll_seconds = max(1.0, float(poll_seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_error: str | None = None
        self._ticks = 0

    def start(self) -> bool:
        import threading

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, name="x-autopilot-scheduler", daemon=True)
            self._thread.start()
            return True

    def stop(self) -> bool:
        with self._lock:
            t = self._thread
            self._stop_event.set()
            self._thread = None
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
            return True
        return False

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            return {
                "running": running,
                "poll_seconds": self._poll_seconds,
                "ticks": self._ticks,
                "last_error": self._last_error,
            }

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick_fn()
                with self._lock:
                    self._last_error = None
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._last_error = str(exc)[:200]
            with self._lock:
                self._ticks += 1
            self._stop_event.wait(self._poll_seconds)
