"""Pure daily action-budget policy over the durable X browser ledger.

The module reads a ledger snapshot only. It never opens X, sleeps, retries,
writes state, or registers a timer. The local accounting day is Asia/Shanghai
(UTC+08:00, no daylight saving since 1991).

Only a completed action with a strictly verified change counts. The four
action types are counted independently, each with a daily goal of 200. The
same account, action type, and target count once. A reply has no state in the
current bridge contract, so it must prove a canonical reply URL plus the
confirmed text and its SHA-256.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from follow_orchestration import (
    derive_from_ledger,
    next_decision,
)

ACTION_TYPES = (
    "follow_profile",
    "like_post",
    "repost_post",
    "reply_post",
)
DAILY_GOAL = 200
LOCAL_TIMEZONE_NAME = "Asia/Shanghai"
LOCAL_TIMEZONE = timezone(timedelta(hours=8), LOCAL_TIMEZONE_NAME)
_VERIFIED_STATES = {
    "follow_profile": "following",
    "like_post": "liked",
    "repost_post": "reposted",
}
_PROFILE_RE = re.compile(
    r"https?://(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})/?",
    re.IGNORECASE,
)
_STATUS_RE = re.compile(
    r"https?://(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})/status/(\d{1,25})/?",
    re.IGNORECASE,
)


def _local_date(timestamp: float) -> date:
    return datetime.fromtimestamp(timestamp, LOCAL_TIMEZONE).date()


def _parse_epoch(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _profile_handle(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = _PROFILE_RE.fullmatch(value.strip())
    return match.group(1).casefold() if match else None


def _status_parts(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    match = _STATUS_RE.fullmatch(value.strip())
    if not match:
        return None
    return match.group(1).casefold(), match.group(2)


def _canonical_status(value: Any) -> str | None:
    parts = _status_parts(value)
    if not parts:
        return None
    return f"https://x.com/{parts[0]}/status/{parts[1]}"


def _target_key(
    action_type: str,
    payload: dict[str, Any],
    result: dict[str, Any],
) -> tuple[str, str] | None:
    """Return (kind, stable target) only when the proof names the target."""

    if action_type == "follow_profile":
        result_handle = _profile_handle(result.get("profile_url"))
        supplied_handle = str(result.get("handle", "")).strip().lstrip("@").casefold()
        payload_handle = _profile_handle(payload.get("profile_url"))
        if payload.get("handle") is not None:
            payload_handle = str(payload.get("handle", "")).strip().lstrip("@").casefold()
        if (
            not result_handle
            or supplied_handle != result_handle
            or payload_handle != result_handle
        ):
            return None
        return "profile", result_handle

    if action_type in {"like_post", "repost_post"}:
        result_status = _status_parts(result.get("post_url"))
        payload_status = _status_parts(payload.get("post_url", payload.get("url")))
        if not result_status or result_status != payload_status:
            return None
        author = str(result.get("author", "")).strip().lstrip("@").casefold()
        payload_author = str(payload.get("author", "")).strip().lstrip("@").casefold()
        if author != result_status[0] or (payload_author and payload_author != author):
            return None
        return "status", f"{result_status[0]}/status/{result_status[1]}"

    if action_type == "reply_post":
        target = _status_parts(payload.get("post_url", payload.get("url")))
        completed_target = _status_parts(result.get("post_url"))
        reply = _status_parts(result.get("reply_url"))
        if not target or target != completed_target or not reply:
            return None
        # The reply URL must be a different canonical status, not the target.
        if reply == target:
            return None
        return "status", f"{reply[0]}/status/{reply[1]}"

    return None


def _reply_text_proven(payload: dict[str, Any], result: dict[str, Any]) -> bool:
    expected = payload.get("text")
    actual = result.get("text")
    if not isinstance(expected, str) or not expected or actual != expected:
        return False
    digest = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    if result.get("text_sha256") != digest:
        return False
    payload_digest = payload.get("text_sha256")
    return payload_digest is None or payload_digest == digest


def _eligible_target(
    action: dict[str, Any],
    action_type: str,
) -> tuple[str, str] | None:
    if action.get("status") != "completed":
        return None
    if action.get("type") != action_type:
        return None
    result = action.get("result")
    payload = action.get("payload")
    if not isinstance(result, dict) or not isinstance(payload, dict):
        return None
    if result.get("verified") is not True or result.get("changed") is not True:
        return None
    if action_type in _VERIFIED_STATES:
        if result.get("state") != _VERIFIED_STATES[action_type]:
            return None
    elif not _reply_text_proven(payload, result):
        return None
    return _target_key(action_type, payload, result)


def _policy_from_ledger(ledger: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Use the same policy derivation for reports and the follow runner."""
    try:
        return derive_from_ledger(ledger), "follow_orchestration.derive_from_ledger"
    except (TypeError, ValueError, KeyError, AttributeError):
        return (
            {
                "mode": "manual_recovery",
                "stage_index": 0,
                "cooldown_until_epoch": None,
                "reason": "rate_gate_derivation_failed",
            },
            "fail_closed_rate_record",
        )


def evaluate_daily_action_budget(
    ledger: dict[str, Any],
    *,
    now_epoch: float | None = None,
    local_day: date | str | None = None,
    fresh_target_available: bool = True,
) -> dict[str, Any]:
    """Return per-action counts plus the durable rate-gate decision.

    The ledger root's ``account_binding`` string is the account identity. A
    missing or blank binding fails closed: no action is counted, because the
    account+type+target deduplication key cannot be established.
    """

    if not isinstance(ledger, dict):
        raise TypeError("ledger must be an object")
    now = float(now_epoch) if now_epoch is not None else datetime.now(tz=LOCAL_TIMEZONE).timestamp()
    if local_day is None:
        day = _local_date(now)
    elif isinstance(local_day, date) and not isinstance(local_day, datetime):
        day = local_day
    elif isinstance(local_day, str):
        day = date.fromisoformat(local_day)
    else:
        raise ValueError("local_day must be a date or ISO date string")

    binding_value = ledger.get("account_binding")
    binding = binding_value.strip() if isinstance(binding_value, str) else ""
    achieved = {action_type: 0 for action_type in ACTION_TYPES}
    seen: set[tuple[str, str, str, str]] = set()
    skipped = {
        "ineligible": 0,
        "outside_local_day": 0,
        "invalid_completed_at": 0,
        "duplicate": 0,
    }

    actions = ledger.get("actions")
    for action in (actions.values() if isinstance(actions, dict) else ()):
        if not isinstance(action, dict) or action.get("type") not in ACTION_TYPES:
            continue
        completed_at = action.get("completed_at")
        completed_epoch = _parse_epoch(completed_at)
        if completed_epoch is None:
            skipped["invalid_completed_at"] += 1
            continue
        if _local_date(completed_epoch) != day:
            skipped["outside_local_day"] += 1
            continue
        target = _eligible_target(action, str(action.get("type")))
        if target is None or not binding:
            skipped["ineligible"] += 1
            continue
        kind, value = target
        key = (binding.casefold(), str(action.get("type")), kind, value)
        if key in seen:
            skipped["duplicate"] += 1
            continue
        seen.add(key)
        action_type = str(action.get("type"))
        achieved[action_type] += 1

    remaining = {
        action_type: max(0, DAILY_GOAL - achieved[action_type])
        for action_type in ACTION_TYPES
    }
    policy, policy_source = _policy_from_ledger(ledger)
    decision = next_decision(
        policy,
        now_epoch=now,
        fresh_target_available=fresh_target_available,
    )
    blocked_detail = None if decision["allow_write"] else {
        "reason": decision["reason"],
        "wait_until_epoch": decision["wait_until_epoch"],
        "calibration_required": decision["calibration_required"],
        "policy_mode": policy.get("mode"),
        "policy_reason": policy.get("reason"),
        "rate_limit_strength": policy.get("rate_limit_strength"),
        "recovery_source": policy.get("recovery_source"),
    }
    if not binding:
        blocked_detail = {
            "reason": "account_binding_missing",
            "wait_until_epoch": None,
            "calibration_required": True,
        }
        decision = {**decision, "allow_write": False, "wave_size": 0, "reason": "account_binding_missing"}

    return {
        "account_binding": binding,
        "local_day": day.isoformat(),
        "timezone": LOCAL_TIMEZONE_NAME,
        "goal": DAILY_GOAL,
        "action_types": list(ACTION_TYPES),
        "achieved": achieved,
        "remaining": remaining,
        "total_achieved": sum(achieved.values()),
        "total_remaining": sum(remaining.values()),
        "goal_met": all(remaining[action_type] == 0 for action_type in ACTION_TYPES),
        "blocked": blocked_detail is not None,
        "blocked_detail": blocked_detail,
        "policy": policy,
        "policy_source": policy_source,
        "decision": decision,
        "skipped": skipped,
    }

