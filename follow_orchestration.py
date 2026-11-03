"""Pure policy for pacing bounded X write waves.

This module does not send requests, sleep, retry an action, or pretend to be a
human. It converts observed account-level evidence into a conservative next
wave decision. The durable browser ledger remains the source of truth.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

WAVE_SIZES = (1, 3, 5, 10)
VALID_MODES = frozenset({"calibrating", "stable", "cooldown", "manual_recovery"})
WRITE_TYPES = frozenset(
    {
        "follow_profile",
        "like_post",
        "repost_post",
        "reply_post",
        "publish_post",
        "delete_post",
    }
)
GLOBAL_STOP_CODES = frozenset(
    {
        "X_SECURITY_INTERSTITIAL",
        "BROWSER_RUNTIME_MISSING",
        "BROWSER_BINARY_MISSING",
        "BROWSER_START_FAILED",
        "BROWSER_EXECUTOR_TIMEOUT",
        "BROWSER_EXECUTOR_STUCK",
        "BROWSER_SESSION_BUSY",
        "BROWSER_OWNER_EXCLUSIVE",
    }
)


def _valid_retry(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) and value >= 0 else None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    timestamp = parsed.timestamp()
    return timestamp if timestamp >= 0 else None


def initial_policy() -> dict[str, Any]:
    return {
        "mode": "calibrating",
        "stage_index": 0,
        "cooldown_until_epoch": None,
        "reason": "initial_calibration",
    }


def _normalise(state: dict[str, Any] | None) -> dict[str, Any]:
    source = dict(state or {})
    stage = source.get("stage_index", 0)
    if not isinstance(stage, int) or isinstance(stage, bool):
        stage = 0
    source["stage_index"] = max(0, min(stage, len(WAVE_SIZES) - 1))
    mode = source.get("mode")
    source["mode"] = mode if mode in VALID_MODES else "manual_recovery"
    if mode not in VALID_MODES:
        source["reason"] = "invalid_policy_state"
    source.setdefault("cooldown_until_epoch", None)
    source.setdefault("reason", "resume")
    return source


def observe_event(
    state: dict[str, Any] | None,
    *,
    code: str,
    observed_at_epoch: float,
    retry_after_seconds: Any = None,
    recovery_until_epoch: Any = None,
    rate_limit_strength: str | None = None,
) -> dict[str, Any]:
    """Apply one observed result and return a new JSON-safe policy state."""

    next_state = _normalise(state)
    if code == "X_RATE_LIMITED":
        next_state["stage_index"] = 0
        next_state["rate_limit_strength"] = rate_limit_strength or "hard_429"
        reset_valid = _valid_retry(recovery_until_epoch)
        retry_valid = _valid_retry(retry_after_seconds) and _valid_retry(observed_at_epoch)
        retry_until = observed_at_epoch + float(retry_after_seconds) if retry_valid else None
        if reset_valid and (retry_until is None or recovery_until_epoch >= retry_until):
            next_state.update(
                mode="cooldown",
                cooldown_until_epoch=float(recovery_until_epoch),
                reason="x_rate_limited_reset_window",
                recovery_source="x-rate-limit-reset",
            )
            return next_state
        if retry_valid:
            next_state.update(
                mode="cooldown",
                cooldown_until_epoch=retry_until,
                reason="x_rate_limited_retry_after",
                recovery_source="retry-after",
            )
        else:
            next_state.update(
                mode="manual_recovery",
                cooldown_until_epoch=None,
                reason="x_rate_limited_without_recovery_time",
                recovery_source=None,
            )
        return next_state
    if code in GLOBAL_STOP_CODES:
        next_state.update(
            mode="manual_recovery",
            cooldown_until_epoch=None,
            reason=f"global_stop:{code}",
        )
        return next_state
    if code == "verified_success":
        next_state["stage_index"] = min(
            next_state["stage_index"] + 1, len(WAVE_SIZES) - 1
        )
        next_state["mode"] = (
            "stable" if next_state["stage_index"] == len(WAVE_SIZES) - 1 else "calibrating"
        )
        next_state["cooldown_until_epoch"] = None
        next_state["reason"] = "verified_wave_success"
        return next_state
    if code in {"ACTION_UNCERTAIN", "FOLLOW_PENDING"}:
        next_state["mode"] = "calibrating"
        next_state["stage_index"] = max(0, min(next_state["stage_index"], 1))
        next_state["reason"] = f"target_local:{code}"
        return next_state
    next_state.update(mode="manual_recovery", cooldown_until_epoch=None, reason=f"stop:{code}")
    return next_state


def next_decision(
    state: dict[str, Any] | None,
    *,
    now_epoch: float,
    fresh_target_available: bool,
) -> dict[str, Any]:
    """Return a conservative, side-effect-free decision for the next wave."""

    current = _normalise(state)
    if not fresh_target_available:
        return {
            "allow_write": False,
            "wave_size": 0,
            "wait_until_epoch": current.get("cooldown_until_epoch"),
            "reason": "no_fresh_target",
            "calibration_required": False,
        }
    if current["mode"] == "manual_recovery":
        return {
            "allow_write": False,
            "wave_size": 0,
            "wait_until_epoch": None,
            "reason": current["reason"],
            "calibration_required": True,
        }
    until = current.get("cooldown_until_epoch")
    if current["mode"] == "cooldown" and not _valid_retry(until):
        return {
            "allow_write": False,
            "wave_size": 0,
            "wait_until_epoch": None,
            "reason": "invalid_cooldown_deadline",
            "calibration_required": True,
        }
    if current["mode"] == "cooldown" and isinstance(until, (int, float)) and now_epoch < until:
        return {
            "allow_write": False,
            "wave_size": 0,
            "wait_until_epoch": until,
            "reason": "cooldown_active",
            "calibration_required": True,
        }
    if current["mode"] == "cooldown":
        return {
            "allow_write": True,
            "wave_size": 1,
            "wait_until_epoch": until,
            "reason": "cooldown_expired_calibration",
            "calibration_required": True,
        }
    wave = WAVE_SIZES[current["stage_index"]]
    return {
        "allow_write": True,
        "wave_size": wave,
        "wait_until_epoch": None,
        "reason": current["reason"],
        "calibration_required": current["mode"] != "stable",
    }


def derive_from_ledger(ledger: dict[str, Any]) -> dict[str, Any]:
    """Derive policy from the durable action ledger without exposing targets."""

    records = []
    actions = ledger.get("actions") or {}
    if not isinstance(actions, dict):
        raise ValueError("actions must be an object")
    for action in actions.values():
        if not isinstance(action, dict):
            raise ValueError("action must be an object")
        if action.get("type") not in WRITE_TYPES:
            continue
        records.append(action)
    records.sort(key=lambda item: _epoch(item.get("completed_at")) or 0)
    state = initial_policy()
    rates = []
    for record in records:
        error = record.get("error") or {}
        if error.get("code") != "X_RATE_LIMITED":
            continue
        details = error.get("details") or {}
        observed = _epoch(record.get("completed_at"))
        rate_limit = details.get("rate_limit") or {}
        reset_epoch = rate_limit.get("reset_epoch")
        # A relative wait without an observed time cannot establish recovery.
        retry = details.get("retry_after_seconds") if observed is not None else None
        rate_state = observe_event(
            state,
            code="X_RATE_LIMITED",
            observed_at_epoch=observed if observed is not None else 0,
            retry_after_seconds=retry,
            recovery_until_epoch=reset_epoch,
            rate_limit_strength=details.get("rate_limit_strength") or "hard_429",
        )
        if rate_state["mode"] == "manual_recovery":
            return rate_state
        rates.append(rate_state)
    # Match the bridge: a newer rate record cannot erase an older gate.
    if rates:
        state = max(rates, key=lambda item: item["cooldown_until_epoch"])
    for record in records:
        code = (record.get("error") or {}).get("code")
        if code in GLOBAL_STOP_CODES:
            return observe_event(state, code=code, observed_at_epoch=0)
    if rates:
        return state
    for record in records:
        result = record.get("result") or {}
        code = (record.get("error") or {}).get("code")
        if code in {"ACTION_UNCERTAIN", "FOLLOW_PENDING"} or record.get("status") == "uncertain":
            state = observe_event(state, code=code or "ACTION_UNCERTAIN", observed_at_epoch=0)
        elif (
            record.get("status") == "completed"
            and result.get("verified") is True
            and result.get("changed") is True
        ):
            state = observe_event(state, code="verified_success", observed_at_epoch=0)
    return state

