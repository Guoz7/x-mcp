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
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


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
        if _valid_retry(recovery_until_epoch):
            next_state.update(
                mode="cooldown",
                cooldown_until_epoch=float(recovery_until_epoch),
                reason="x_rate_limited_reset_window",
                recovery_source="x-rate-limit-reset",
            )
            return next_state
        if _valid_retry(retry_after_seconds):
            next_state.update(
                mode="cooldown",
                cooldown_until_epoch=observed_at_epoch + float(retry_after_seconds),
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
    for action in (ledger.get("actions") or {}).values():
        if action.get("type") not in WRITE_TYPES:
            continue
        records.append(action)
    records.sort(key=lambda item: _epoch(item.get("completed_at")) or 0)
    latest_rate = next(
        (item for item in reversed(records) if (item.get("error") or {}).get("code") == "X_RATE_LIMITED"),
        None,
    )
    state = initial_policy()
    if latest_rate is not None:
        error = latest_rate.get("error") or {}
        details = error.get("details") or {}
        observed = _epoch(latest_rate.get("completed_at")) or 0
        rate_limit = details.get("rate_limit") or {}
        reset_epoch = rate_limit.get("reset_epoch")
        return observe_event(
            state,
            code="X_RATE_LIMITED",
            observed_at_epoch=observed,
            retry_after_seconds=details.get("retry_after_seconds"),
            recovery_until_epoch=reset_epoch,
            rate_limit_strength=details.get("rate_limit_strength") or "hard_429",
        )
    successes = [
        item for item in records
        if item.get("status") == "completed"
        and (item.get("result") or {}).get("verified") is True
    ]
    for _ in range(min(len(successes), len(WAVE_SIZES) - 1)):
        state = observe_event(state, code="verified_success", observed_at_epoch=0)
    return state

