"""Read-only audit of a persisted follow-run browser ledger.

The audit never mutates the ledger, opens a browser, retries an action, or
promotes an uncertain result. Output uses stable hashes instead of handles,
URLs, claim tokens, or session ids.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any

FOLLOW = "follow_profile"
OUTCOME_EVENTS = {
    "completed": "action_completed",
    "uncertain": "action_uncertain",
    "failed": "action_failed",
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _ref(namespace: str, value: Any) -> str:
    material = f"{namespace}\0{value}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:24]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _timing(action: dict[str, Any]) -> dict[str, Any]:
    result = action.get("result") if isinstance(action.get("result"), dict) else {}
    error = action.get("error") if isinstance(action.get("error"), dict) else {}
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    exact = _number(result.get("elapsed_seconds"))
    if exact is not None:
        return {
            "seconds": exact,
            "source": "result.elapsed_seconds",
            "browser_timing": True,
        }
    precise = _number(details.get("attempt_elapsed_seconds"))
    if precise is not None:
        return {
            "seconds": precise,
            "source": "error.details.attempt_elapsed_seconds",
            "browser_timing": False,
            "scope": "executor_or_server_attempt",
        }
    claimed = _epoch(action.get("claimed_at"))
    completed = _epoch(action.get("completed_at"))
    if claimed is not None and completed is not None and completed >= claimed:
        return {
            "seconds": round(completed - claimed, 3),
            "source": "ledger_timestamp_elapsed",
            "browser_timing": False,
            "resolution": "seconds",
        }
    return {
        "seconds": None,
        "source": None,
        "browser_timing": False,
        "unknown": True,
        "reason": (
            "claimed_unresolved"
            if action.get("status") == "claimed"
            else "timing_not_recorded"
        ),
    }


def _strict_follow(action: dict[str, Any]) -> bool:
    result = action.get("result")
    return (
        action.get("type") == FOLLOW
        and action.get("status") == "completed"
        and isinstance(result, dict)
        and result.get("verified") is True
        and result.get("changed") is True
        and result.get("state") == "following"
    )


def _target(action: dict[str, Any]) -> str | None:
    result = action.get("result") if isinstance(action.get("result"), dict) else {}
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    handle = result.get("handle", payload.get("handle"))
    if not isinstance(handle, str) or not handle.strip():
        profile = result.get("profile_url", payload.get("profile_url"))
        if not isinstance(profile, str) or not profile.rstrip("/").rsplit("/", 1)[-1]:
            return None
        handle = profile.rstrip("/").rsplit("/", 1)[-1]
    return handle.strip().lstrip("@").casefold()


def _verify_chain(evidence: Any) -> dict[str, Any]:
    if not isinstance(evidence, list):
        return {
            "valid": False,
            "entries": 0,
            "invalid_sequences": [],
            "reason": "evidence_not_an_array",
        }
    prior_hash = "0" * 64
    prior_sequence = 0
    invalid: list[int] = []
    trusted: list[dict[str, Any]] = []
    for index, entry in enumerate(evidence, start=1):
        if not isinstance(entry, dict):
            invalid.append(index)
            prior_hash = ""
            continue
        candidate = dict(entry)
        actual = candidate.pop("hash", None)
        sequence = entry.get("sequence")
        valid = (
            isinstance(sequence, int)
            and not isinstance(sequence, bool)
            and sequence == prior_sequence + 1
            and entry.get("prev_hash") == prior_hash
            and isinstance(actual, str)
            and actual == _digest(candidate)
        )
        if not valid:
            invalid.append(index)
        else:
            trusted.append(entry)
        prior_sequence = sequence if isinstance(sequence, int) and not isinstance(sequence, bool) else prior_sequence
        prior_hash = actual if isinstance(actual, str) else ""
    return {
        "valid": not invalid and len(trusted) == len(evidence),
        "entries": len(evidence),
        "invalid_sequences": invalid,
        "trusted_entries": len(trusted),
        "trusted": trusted,
    }


def _evidence_comparison(
    actions: list[dict[str, Any]], chain: dict[str, Any]
) -> dict[str, Any]:
    if not chain["valid"]:
        return {
            "available": False,
            "outcomes_compared": 0,
            "result_evidence_mismatches": 0,
            "missing_outcome_evidence": 0,
            "reason": "evidence_chain_invalid",
        }
    last: dict[str, dict[str, Any]] = {}
    for entry in chain["trusted"]:
        subject = entry.get("subject_id")
        if entry.get("type") in OUTCOME_EVENTS.values() and isinstance(subject, str):
            last[subject] = entry
    compared = 0
    mismatches = 0
    missing = 0
    for action in actions:
        action_id = action.get("action_id")
        status = action.get("status")
        if status not in {"completed", "uncertain"} or not isinstance(action_id, str):
            continue
        event = last.get(action_id)
        if event is None:
            missing += 1
            continue
        compared += 1
        detail = event.get("detail")
        if (
            event.get("type") != OUTCOME_EVENTS[status]
            or not isinstance(detail, dict)
            or detail.get("result") != action.get("result")
            or detail.get("error") != action.get("error")
        ):
            mismatches += 1
    return {
        "available": True,
        "outcomes_compared": compared,
        "result_evidence_mismatches": mismatches,
        "missing_outcome_evidence": missing,
    }


def audit_follow_run(ledger: dict[str, Any]) -> dict[str, Any]:
    """Return a private-data-free audit of one ledger snapshot."""

    if not isinstance(ledger, dict):
        raise TypeError("ledger must be an object")
    raw_actions = ledger.get("actions")
    actions = [
        item for item in (raw_actions.values() if isinstance(raw_actions, dict) else ())
        if isinstance(item, dict)
    ]
    actions.sort(key=lambda item: str(item.get("action_id", "")))

    status_counts: dict[str, int] = {}
    follow_status_counts: dict[str, int] = {}
    error_counts: dict[str, int] = {}
    for action in actions:
        status = str(action.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1
        if action.get("type") == FOLLOW:
            follow_status_counts[status] = follow_status_counts.get(status, 0) + 1
        error = action.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        if isinstance(code, str) and code:
            error_counts[code] = error_counts.get(code, 0) + 1

    seen: set[str] = set()
    unique_verified = 0
    duplicate_verified = 0
    rows: list[dict[str, Any]] = []
    handoff_requested = 0
    visual_execution_claims = 0
    screenshot_success_verified = 0
    for action in actions:
        is_follow = action.get("type") == FOLLOW
        error = action.get("error") if isinstance(action.get("error"), dict) else {}
        details = error.get("details") if isinstance(error.get("details"), dict) else {}
        fallback = details.get("visual_fallback")
        handoff = is_follow and isinstance(fallback, dict) and fallback.get("required") is True
        if handoff:
            handoff_requested += 1
        execution = action.get("execution") if isinstance(action.get("execution"), dict) else {}
        strategy = execution.get("locator_strategy")
        visual_execution = is_follow and strategy in {"screenshot", "coordinate"}
        if visual_execution:
            visual_execution_claims += 1
        strict = is_follow and _strict_follow(action)
        target = _target(action) if is_follow else None
        duplicate = False
        if strict and target is not None:
            if target in seen:
                duplicate = True
                duplicate_verified += 1
            else:
                seen.add(target)
                unique_verified += 1
        if strict and visual_execution:
            # The ledger does not record a separate visual postcondition.
            # Keep this count but do not call it proven fallback success.
            screenshot_success_verified += 1
        rows.append(
            {
                "action_ref": _ref("action", action.get("action_id", "")),
                "type": action.get("type"),
                "is_follow": is_follow,
                "target_ref": _ref("follow-target", target) if target else None,
                "status": str(action.get("status", "unknown")),
                "strict_verified_following": strict,
                "duplicate_verified_target": duplicate,
                "verified": bool(
                    isinstance(action.get("result"), dict)
                    and action["result"].get("verified") is True
                ),
                "changed": bool(
                    isinstance(action.get("result"), dict)
                    and action["result"].get("changed") is True
                ),
                "state": (
                    action["result"].get("state")
                    if isinstance(action.get("result"), dict)
                    else None
                ),
                "error_code": error.get("code"),
                "timing": _timing(action),
                "visual_handoff_requested": handoff,
                "locator_strategy": strategy,
                "visual_execution_claim": visual_execution,
            }
        )

    chain = _verify_chain(ledger.get("evidence"))
    comparison = _evidence_comparison(actions, chain)
    return {
        "scope": {
            "action_types": [FOLLOW],
            "status_aggregates_all_actions": True,
            "read_only": True,
        },
        "status_counts": dict(sorted(status_counts.items())),
        "follow_status_counts": dict(sorted(follow_status_counts.items())),
        "error_counts": dict(sorted(error_counts.items())),
        "follow": {
            "strict_verified_changed_following_unique": unique_verified,
            "duplicate_verified_targets": duplicate_verified,
            "handoff_requested": handoff_requested,
            "visual_execution_claims": visual_execution_claims,
            "visual_fallback_success_proven": 0,
            "screenshot_or_coordinate_completed": screenshot_success_verified,
        },
        "timing_note": (
            "ledger_timestamp_elapsed uses claimed_at to completed_at with "
            "second resolution; it is not browser-measured time"
        ),
        "visual_note": (
            "screenshot/coordinate locator_strategy or a handoff request does "
            "not establish fallback success"
        ),
        "evidence": {
            "chain": {
                key: chain[key]
                for key in ("valid", "entries", "invalid_sequences", "trusted_entries", "reason")
                if key in chain
            },
            "comparison": comparison,
        },
        "rows": rows,
    }


def audit_file(path: str) -> dict[str, Any]:
    """Read one JSON ledger and audit it without writing anything."""

    with open(path, "r", encoding="utf-8") as handle:
        return audit_follow_run(json.load(handle))

