"""Personal X account health, safe pacing, content queue, and daily orchestration.

This module provides the policy and state-derivation layer for maintaining a
personal X account safely:

1. Circuit breaker & safe pacing enforcement (from `docs/orchestration-profile-safe.json`
   and `account_state.py`), stopping immediately on `141`, `37`, `283`, `161`,
   429 rate-limits, or consecutive `uncertain` outcomes.
2. Account standing & warmup health score (`0-100`) combining profile readiness,
   original-content cadence, balanced engagement, and safety cleanliness.
3. Durable personal content queue (`content_queue.json`) for scheduled original
   posts and replies.
4. Unified daily activity planner (`build_daily_orchestration_plan`) that
   combines due original posts, deduplicated timeline likes/reposts, and
   deduplicated follow candidates into a single human-paced, bounded wave.
"""

from __future__ import annotations

import json
import os
import random
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from account_state import classify_refusal
from daily_action_budget import (
    LOCAL_TIMEZONE,
    _local_date,
    _parse_epoch,
    evaluate_daily_action_budget,
)
from ledger_lock import LedgerLock

ROOT = Path(__file__).resolve().parent
DEFAULT_SAFE_PROFILE_PATH = ROOT / "docs" / "orchestration-profile-safe.json"

DEFAULT_SAFE_CAPS: dict[str, dict[str, Any]] = {
    "follow_profile": {"enabled": True, "per_day": 25, "min_gap_seconds": 90, "max_gap_seconds": 240},
    "repost_post": {"enabled": True, "per_day": 40, "min_gap_seconds": 120, "max_gap_seconds": 300},
    "like_post": {"enabled": True, "per_day": 60, "min_gap_seconds": 60, "max_gap_seconds": 180},
    "reply_post": {"enabled": True, "per_day": 15, "min_gap_seconds": 120, "max_gap_seconds": 300},
    "publish_post": {"enabled": True, "per_day": 5, "min_gap_seconds": 600, "max_gap_seconds": 1800},
}

DEFAULT_CIRCUIT_BREAKER: dict[str, Any] = {
    "stop_on_any_refusal": True,
    "refusal_codes": ["283", "161", "141", "37"],
    "stop_on_uncertain_streak": 2,
    "cooldown_hours_after_refusal": 24,
    "cooldown_hours_after_uncertain_streak": 4,
    "do_not_switch_action_kind": True,
}

DEFAULT_WARMUP: dict[str, Any] = {
    "required_days_before_automation": 7,
    "required_original_posts": 10,
    "required_profile_fields": ["name", "bio", "avatar"],
}

_SECRET_PATTERN = re.compile(
    r"(?:sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]+PRIVATE KEY-----)"
)
_EXTERNAL_URL_RE = re.compile(r"https?://(?!(?:www\.)?(?:x\.com|twitter\.com|t\.co)\b)\S+", re.IGNORECASE)
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_AI_CLICHE_PATTERNS = (
    "great post",
    "very insightful",
    "thanks for sharing",
    "totally agree",
    "well said",
    "this is huge",
    "game changer",
    "感谢分享",
    "很有深度",
    "说得太好了",
    "非常赞同",
    "受益匪浅",
)


def check_reply_naturalness(text: str) -> list[str]:
    """Return warnings if a reply reads like generic AI boilerplate."""
    cleaned = str(text or "").strip()
    lower = cleaned.casefold()
    warnings: list[str] = []
    for phrase in _AI_CLICHE_PATTERNS:
        if phrase in lower:
            warnings.append(f"Avoid generic AI cliché: '{phrase}'")
    if cleaned.count("#") >= 2:
        warnings.append("Avoid multiple hashtags in replies.")
    if len(cleaned) < 8:
        warnings.append("Reply is too short to add a concrete point.")
    return warnings
_STATUS_URL_RE = re.compile(
    r"^https?://(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})/status/(\d{1,25})(?:\?.*)?$",
    re.IGNORECASE,
)


class OrchestratorError(Exception):
    """Structured error safe to surface through MCP."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def load_safe_profile(path: Path | None = None) -> dict[str, Any]:
    """Load the safe pacing profile, falling back to built-in defaults if missing."""
    target = path or DEFAULT_SAFE_PROFILE_PATH
    raw: dict[str, Any] = {}
    if target.is_file():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except ValueError:
            raw = {}

    actions: dict[str, dict[str, Any]] = {}
    key_map = {
        "follow_profile": "follow",
        "repost_post": "repost",
        "like_post": "like",
        "reply_post": "reply",
        "publish_post": "publish",
    }
    for action_type, short_key in key_map.items():
        base = dict(DEFAULT_SAFE_CAPS[action_type])
        override = raw.get(short_key)
        if isinstance(override, dict):
            for field in ("enabled", "per_day", "min_gap_seconds", "max_gap_seconds", "per_wave"):
                if field in override and override[field] is not None:
                    base[field] = override[field]
        actions[action_type] = base

    cb = dict(DEFAULT_CIRCUIT_BREAKER)
    if isinstance(raw.get("circuit_breaker"), dict):
        cb.update(raw["circuit_breaker"])

    warmup = dict(DEFAULT_WARMUP)
    if isinstance(raw.get("warmup"), dict):
        warmup.update(raw["warmup"])

    return {
        "profile_version": raw.get("profile_version", 2),
        "actions": actions,
        "circuit_breaker": cb,
        "warmup": warmup,
    }


def _sorted_actions(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    actions = ledger.get("actions")
    if not isinstance(actions, dict):
        return []
    rows = [item for item in actions.values() if isinstance(item, dict)]
    rows.sort(
        key=lambda item: (
            _parse_epoch(item.get("completed_at"))
            or _parse_epoch(item.get("created_at"))
            or 0.0,
            str(item.get("action_id") or ""),
        )
    )
    return rows


def evaluate_circuit_breaker(
    ledger: dict[str, Any],
    *,
    profile: dict[str, Any] | None = None,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Inspect durable ledger refusals and uncertain streaks for write safety."""
    now = float(now_epoch) if now_epoch is not None else time.time()
    prof = profile or load_safe_profile()
    cb = prof["circuit_breaker"]
    refusal_cooldown_sec = float(cb.get("cooldown_hours_after_refusal", 24)) * 3600.0
    uncertain_cooldown_sec = float(cb.get("cooldown_hours_after_uncertain_streak", 4)) * 3600.0
    uncertain_limit = int(cb.get("stop_on_uncertain_streak", 2))

    rows = _sorted_actions(ledger)
    latest_refusal: dict[str, Any] | None = None
    trailing_uncertain = 0
    latest_uncertain_at: float | None = None

    for action in rows:
        status = action.get("status")
        error = action.get("error") or {}
        details = error.get("details") or {}
        rejection = details.get("rejection")
        verdict = classify_refusal(rejection if isinstance(rejection, dict) else None)

        if verdict == "suspended":
            return {
                "status": "suspended",
                "allow_write": False,
                "reason": "ACCOUNT_SUSPENDED",
                "cooldown_until_epoch": None,
                "refusal": {
                    "action_id": action.get("action_id"),
                    "code": (rejection or {}).get("code"),
                    "message": str((rejection or {}).get("message") or "")[:160],
                    "at": action.get("completed_at"),
                },
                "uncertain_streak": trailing_uncertain,
            }

        if verdict in {"policy", "allowance"}:
            at_epoch = _parse_epoch(action.get("completed_at")) or _parse_epoch(action.get("created_at"))
            latest_refusal = {
                "action_id": action.get("action_id"),
                "verdict": verdict,
                "code": (rejection or {}).get("code"),
                "message": str((rejection or {}).get("message") or "")[:160],
                "at_epoch": at_epoch,
            }

        if status == "uncertain":
            trailing_uncertain += 1
            latest_uncertain_at = _parse_epoch(action.get("completed_at")) or _parse_epoch(action.get("created_at"))
        elif status == "completed":
            trailing_uncertain = 0
            latest_uncertain_at = None

    if latest_refusal and latest_refusal.get("at_epoch") is not None:
        until = float(latest_refusal["at_epoch"]) + refusal_cooldown_sec
        if now < until:
            return {
                "status": "cooldown",
                "allow_write": False,
                "reason": "REFUSAL_COOLDOWN",
                "cooldown_until_epoch": until,
                "refusal": latest_refusal,
                "uncertain_streak": trailing_uncertain,
            }

    if trailing_uncertain >= uncertain_limit and latest_uncertain_at is not None:
        until = latest_uncertain_at + uncertain_cooldown_sec
        if now < until:
            return {
                "status": "cooldown",
                "allow_write": False,
                "reason": "UNCERTAIN_STREAK_COOLDOWN",
                "cooldown_until_epoch": until,
                "refusal": None,
                "uncertain_streak": trailing_uncertain,
            }

    # Respect X_RATE_LIMITED cooldown derived by daily_action_budget / follow_orchestration.
    if ledger.get("account_binding"):
        budget = evaluate_daily_action_budget(ledger, now_epoch=now)
        if budget.get("blocked"):
            detail = budget.get("blocked_detail") or {}
            if detail.get("reason") in {"cooldown_active", "manual_recovery_required"}:
                return {
                    "status": "cooldown",
                    "allow_write": False,
                    "reason": str(detail.get("reason") or "RATE_LIMIT_COOLDOWN").upper(),
                    "cooldown_until_epoch": detail.get("wait_until_epoch"),
                    "refusal": None,
                    "uncertain_streak": trailing_uncertain,
                }

    return {
        "status": "active",
        "allow_write": True,
        "reason": "healthy",
        "cooldown_until_epoch": None,
        "refusal": None,
        "uncertain_streak": trailing_uncertain,
    }


def _count_today_publishes(ledger: dict[str, Any], now_epoch: float) -> int:
    day = _local_date(now_epoch)
    count = 0
    for action in _sorted_actions(ledger):
        if action.get("type") != "publish_post" or action.get("status") != "completed":
            continue
        result = action.get("result") or {}
        if result.get("verified") is not True:
            continue
        at_epoch = _parse_epoch(action.get("completed_at"))
        if at_epoch is not None and _local_date(at_epoch) == day:
            count += 1
    return count


def evaluate_account_health(
    ledger: dict[str, Any],
    *,
    account_snapshot: dict[str, Any] | None = None,
    content_queue: list[dict[str, Any]] | None = None,
    profile: dict[str, Any] | None = None,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Compute standing score (0-100), warmup readiness, safe daily budget, and next steps."""
    now = float(now_epoch) if now_epoch is not None else time.time()
    prof = profile or load_safe_profile()
    breaker = evaluate_circuit_breaker(ledger, profile=prof, now_epoch=now)
    budget = evaluate_daily_action_budget(ledger, now_epoch=now)

    achieved_today: dict[str, int] = dict(budget["achieved"])
    achieved_today["publish_post"] = _count_today_publishes(ledger, now)

    safe_caps: dict[str, int] = {}
    remaining_safe: dict[str, int] = {}
    for action_type, cfg in prof["actions"].items():
        cap = int(cfg.get("per_day", 0)) if cfg.get("enabled", True) else 0
        safe_caps[action_type] = cap
        remaining_safe[action_type] = max(0, cap - achieved_today.get(action_type, 0))

    # Profile & warmup readiness.
    snap = account_snapshot if isinstance(account_snapshot, dict) else {}
    has_snapshot = bool(snap)
    missing_fields: list[str] = []
    if has_snapshot:
        if not str(snap.get("display_name") or snap.get("name") or "").strip():
            missing_fields.append("name")
        if not str(snap.get("bio") or "").strip():
            missing_fields.append("bio")
        if snap.get("has_avatar") is False:
            missing_fields.append("avatar")
        profile_score = max(0, 20 - len(missing_fields) * 7)
    else:
        profile_score = 10  # Neutral when live snapshot wasn't supplied.

    # Original content score (0-30).
    warmup_cfg = prof["warmup"]
    req_posts = int(warmup_cfg.get("required_original_posts", 10))
    req_days = int(warmup_cfg.get("required_days_before_automation", 7))
    ledger_publishes = sum(
        1 for a in _sorted_actions(ledger)
        if a.get("type") == "publish_post" and a.get("status") == "completed"
    )
    posts_count = int(snap.get("posts_count", ledger_publishes)) if snap.get("posts_count") is not None else ledger_publishes
    active_days = {
        _local_date(ep)
        for a in _sorted_actions(ledger)
        for ep in (_parse_epoch(a.get("completed_at")),)
        if ep is not None
    }
    account_age_days = int(snap.get("account_age_days", len(active_days))) if snap.get("account_age_days") is not None else len(active_days)
    original_ratio = min(1.0, posts_count / max(1, req_posts))
    age_ratio = min(1.0, account_age_days / max(1, req_days))
    original_score = round(20 * original_ratio + 10 * age_ratio)

    # Engagement balance score (0-25).
    total_writes_today = sum(achieved_today.values())
    over_cap = any(achieved_today.get(k, 0) > safe_caps.get(k, 0) for k in safe_caps)
    if over_cap:
        engagement_score = 10
    elif total_writes_today > 0:
        engagement_score = min(25, 15 + min(10, total_writes_today // 3))
    else:
        engagement_score = 15  # Clean baseline ready for daily activity.

    # Safety cleanliness score (0-25).
    if breaker["status"] == "suspended":
        safety_score = 0
    elif breaker["status"] == "cooldown":
        safety_score = 5
    else:
        safety_score = max(0, 25 - breaker["uncertain_streak"] * 8)

    standing_score = max(0, min(100, profile_score + original_score + engagement_score + safety_score))
    warmup_ready = (posts_count >= req_posts) and (account_age_days >= req_days) and not missing_fields
    established_on_x = posts_count >= 50

    if account_age_days < 3 and posts_count < 3:
        warmup_stage = "ghost"
        stage_caps = {"publish_post": 0, "like_post": 3, "follow_profile": 0, "repost_post": 0, "reply_post": 0}
    elif account_age_days < 7 and posts_count < 10:
        warmup_stage = "light_engage"
        stage_caps = {"publish_post": 1, "like_post": 6, "follow_profile": 2, "repost_post": 1, "reply_post": 0}
    elif not warmup_ready and not established_on_x:
        warmup_stage = "content_build"
        stage_caps = {"publish_post": 2, "like_post": 15, "follow_profile": 5, "repost_post": 3, "reply_post": 2}
    else:
        warmup_stage = "growth"
        stage_caps = {"publish_post": 5, "like_post": 60, "follow_profile": 25, "repost_post": 40, "reply_post": 15}

    followers_snap = snap.get("followers_count")
    following_snap = snap.get("following_count")
    ratio_guard = (
        isinstance(following_snap, (int, float))
        and isinstance(followers_snap, (int, float))
        and following_snap >= 50
        and following_snap > max(1.0, float(followers_snap)) * 2.5
    )
    if ratio_guard:
        stage_caps["follow_profile"] = 0

    queued_items = [item for item in (content_queue or []) if item.get("status") == "queued"]
    due_items = [
        item for item in queued_items
        if item.get("scheduled_after_epoch") is None or float(item["scheduled_after_epoch"]) <= now
    ]

    recommended_actions: list[dict[str, Any]] = []
    if not breaker["allow_write"]:
        recommended_actions.append({
            "action": "wait_cooldown",
            "priority": "high",
            "reason": breaker["reason"],
            "cooldown_until_epoch": breaker["cooldown_until_epoch"],
        })
    else:
        if missing_fields:
            recommended_actions.append({
                "action": "complete_profile",
                "priority": "high",
                "missing_fields": missing_fields,
            })
        if due_items and remaining_safe["publish_post"] > 0:
            recommended_actions.append({
                "action": "publish_post",
                "priority": "high",
                "suggested_count": min(len(due_items), remaining_safe["publish_post"], 2),
                "min_gap_seconds": prof["actions"]["publish_post"]["min_gap_seconds"],
            })
        elif posts_count < req_posts and remaining_safe["publish_post"] > 0:
            recommended_actions.append({
                "action": "queue_original_post",
                "priority": "high",
                "reason": f"Build original post standing ({posts_count}/{req_posts}).",
            })
        if remaining_safe["like_post"] > 0:
            recommended_actions.append({
                "action": "like_post",
                "priority": "medium",
                "suggested_count": min(10, remaining_safe["like_post"]),
                "min_gap_seconds": prof["actions"]["like_post"]["min_gap_seconds"],
            })
        if ratio_guard:
            recommended_actions.append({
                "action": "improve_follower_ratio",
                "priority": "high",
                "reason": f"Following ({int(following_snap)}) > 2.5x followers ({int(followers_snap)}); pause follows and focus on original posts.",
            })
        if remaining_safe["follow_profile"] > 0 and stage_caps["follow_profile"] > 0:
            recommended_actions.append({
                "action": "follow_profile",
                "priority": "medium",
                "suggested_count": min(5, remaining_safe["follow_profile"], stage_caps["follow_profile"]),
                "min_gap_seconds": prof["actions"]["follow_profile"]["min_gap_seconds"],
            })
        if remaining_safe["repost_post"] > 0:
            recommended_actions.append({
                "action": "repost_post",
                "priority": "low",
                "suggested_count": min(3, remaining_safe["repost_post"]),
                "min_gap_seconds": prof["actions"]["repost_post"]["min_gap_seconds"],
            })

    return {
        "account_status": breaker["status"],
        "allow_write": breaker["allow_write"],
        "circuit_breaker": breaker,
        "standing_score": standing_score,
        "score_breakdown": {
            "profile_readiness": profile_score,
            "original_content": original_score,
            "engagement_balance": engagement_score,
            "safety_cleanliness": safety_score,
            "snapshot_provided": has_snapshot,
        },
        "warmup": {
            "ready": warmup_ready,
            "stage": warmup_stage,
            "stage_caps": stage_caps,
            "ratio_guard_triggered": ratio_guard,
            "posts_count": posts_count,
            "required_original_posts": req_posts,
            "active_days": account_age_days,
            "required_days": req_days,
            "missing_profile_fields": missing_fields,
        },
        "today": {
            "local_day": budget["local_day"],
            "achieved": achieved_today,
            "safe_caps": safe_caps,
            "remaining_safe": remaining_safe,
        },
        "content_queue": {
            "queued": len(queued_items),
            "due": len(due_items),
        },
        "recommended_actions": recommended_actions,
    }


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class ContentQueueStore:
    """Durable JSON store for scheduled/queued personal X posts and replies."""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "content_queue.json"
        self.lock = LedgerLock(self.state_dir / ".content_queue.lock")

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "items": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data
        except ValueError:
            pass
        return {"version": 1, "items": []}

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(
        self,
        *,
        kind: str,
        text: str,
        topic: str | None = None,
        target_url: str | None = None,
        scheduled_after_epoch: float | None = None,
    ) -> dict[str, Any]:
        if kind not in {"original_post", "reply"}:
            raise OrchestratorError("INVALID_ARGUMENT", "kind must be original_post or reply.")
        cleaned = str(text or "").strip()
        if not cleaned or len(cleaned) > 280:
            raise OrchestratorError("INVALID_ARGUMENT", "text must be 1 to 280 characters.")
        if _SECRET_PATTERN.search(cleaned):
            raise OrchestratorError("SECRET_DETECTED", "Refusing to store text matching a secret pattern.")
        if kind == "reply":
            if not target_url or not _STATUS_URL_RE.match(str(target_url).strip()):
                raise OrchestratorError("INVALID_ARGUMENT", "reply items require a valid X status target_url.")
            cliches = check_reply_naturalness(cleaned)
            if cliches:
                raise OrchestratorError("REPLY_TOO_GENERIC", "; ".join(cliches))
        item = {
            "item_id": "cq_" + secrets.token_hex(6),
            "kind": kind,
            "text": cleaned,
            "topic": str(topic).strip()[:80] if topic else None,
            "target_url": str(target_url).strip() if target_url else None,
            "scheduled_after_epoch": float(scheduled_after_epoch) if scheduled_after_epoch is not None else None,
            "status": "queued",
            "created_at": _iso(time.time()),
            "published_url": None,
            "action_id": None,
        }
        with self.lock:
            data = self._read()
            norm = cleaned.casefold()
            for existing in data["items"]:
                if existing.get("status") in {"queued", "published"} and str(existing.get("text") or "").strip().casefold() == norm:
                    raise OrchestratorError("DUPLICATE_CONTENT", "Matching text already exists in the content queue.")
            data["items"].append(item)
            self._write(data)
        return item

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            items = list(self._read()["items"])
        if status:
            return [item for item in items if item.get("status") == status]
        return items

    def due(self, *, now_epoch: float | None = None) -> list[dict[str, Any]]:
        now = float(now_epoch) if now_epoch is not None else time.time()
        queued = self.list(status="queued")
        return [
            item for item in queued
            if item.get("scheduled_after_epoch") is None or float(item["scheduled_after_epoch"]) <= now
        ]

    def remove(self, item_id: str) -> dict[str, Any]:
        with self.lock:
            data = self._read()
            for item in data["items"]:
                if item.get("item_id") == item_id:
                    item["status"] = "cancelled"
                    self._write(data)
                    return item
        raise OrchestratorError("QUEUE_ITEM_NOT_FOUND", f"Content queue item {item_id} not found.")

    def mark_published(
        self,
        item_id: str,
        *,
        published_url: str | None = None,
        action_id: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            data = self._read()
            for item in data["items"]:
                if item.get("item_id") == item_id:
                    item["status"] = "published"
                    item["published_at"] = _iso(time.time())
                    if published_url:
                        item["published_url"] = published_url
                    if action_id:
                        item["action_id"] = action_id
                    self._write(data)
                    return item
        raise OrchestratorError("QUEUE_ITEM_NOT_FOUND", f"Content queue item {item_id} not found.")


def _seen_ledger_targets(ledger: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Return (seen_handles, seen_status_urls) from completed or active ledger actions."""
    handles: set[str] = set()
    urls: set[str] = set()
    for action in _sorted_actions(ledger):
        if action.get("status") not in {"completed", "pending", "claimed"}:
            continue
        payload = action.get("payload") or {}
        if action.get("type") == "follow_profile":
            handle = str(payload.get("handle") or "").strip().lstrip("@").casefold()
            if handle:
                handles.add(handle)
        elif action.get("type") in {"like_post", "repost_post", "reply_post"}:
            url = str(payload.get("post_url") or payload.get("url") or "").strip()
            match = _STATUS_URL_RE.match(url)
            if match:
                urls.add(f"https://x.com/{match.group(1).casefold()}/status/{match.group(2)}")
    return handles, urls


def score_follow_candidate(cand: dict[str, Any]) -> tuple[int, str]:
    """Score a follow candidate for reciprocal growth likelihood (higher is better)."""
    score = 0
    if cand.get("mutual_verified") is True:
        score += 40
    if str(cand.get("bio") or "").strip():
        score += 10
    if cand.get("recent_post_at") or cand.get("active") is True:
        score += 10
    followers = cand.get("followers_count")
    following = cand.get("following_count")
    if isinstance(followers, (int, float)) and isinstance(following, (int, float)) and following > 0:
        ratio = followers / following
        if 0.2 <= ratio <= 3.0:
            score += 25
        elif followers > 10000 and ratio > 10.0:
            score -= 25
    handle = str(cand.get("handle") or cand.get("username") or "").strip().lstrip("@").casefold()
    return (score, handle)


def score_post_relevance(
    post: dict[str, Any],
    *,
    persona_topics: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
) -> tuple[bool, int]:
    """Return (allowed, relevance_score) for a timeline/search post."""
    text = str(post.get("text") or "").casefold()
    excludes = [str(k).strip().casefold() for k in (exclude_keywords or []) if str(k).strip()]
    if any(k in text for k in excludes):
        return (False, -100)
    topics = [str(k).strip().casefold() for k in (persona_topics or []) if str(k).strip()]
    if not topics:
        return (True, 0)
    hits = sum(1 for k in topics if k in text)
    return (True, hits * 10)


def build_daily_orchestration_plan(
    ledger: dict[str, Any],
    *,
    goals: dict[str, int] | None = None,
    timeline_posts: list[dict[str, Any]] | None = None,
    follow_candidates: list[dict[str, Any]] | None = None,
    content_queue: list[dict[str, Any]] | None = None,
    account_snapshot: dict[str, Any] | None = None,
    persona_topics: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
    profile: dict[str, Any] | None = None,
    now_epoch: float | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Build a deduplicated, safe-paced, mixed daily maintenance plan."""
    now = float(now_epoch) if now_epoch is not None else time.time()
    prof = profile or load_safe_profile()
    health = evaluate_account_health(
        ledger,
        account_snapshot=account_snapshot,
        content_queue=content_queue,
        profile=prof,
        now_epoch=now,
    )

    if not health["allow_write"]:
        return {
            "status": "blocked",
            "blocked": True,
            "reason": health["circuit_breaker"]["reason"],
            "cooldown_until_epoch": health["circuit_breaker"]["cooldown_until_epoch"],
            "health": health,
            "operation_count": 0,
            "operations": [],
            "estimated_duration_seconds": 0,
        }

    requested = goals or {
        "publish_post": 1,
        "like_post": 10,
        "follow_profile": 5,
        "repost_post": 3,
        "reply_post": 2,
    }
    stage = str(health["warmup"].get("stage") or "growth")
    stage_caps = dict(health["warmup"].get("stage_caps") or {})
    remaining = health["today"]["remaining_safe"]
    capped = {
        kind: max(0, min(int(requested.get(kind, 0)), int(remaining.get(kind, 0)), int(stage_caps.get(kind, 0))))
        for kind in ("publish_post", "like_post", "follow_profile", "repost_post", "reply_post")
    }

    seen_handles, seen_urls = _seen_ledger_targets(ledger)
    rng = random.Random(seed) if seed is not None else random.SystemRandom()

    buckets: dict[str, list[dict[str, Any]]] = {
        "publish_post": [],
        "like_post": [],
        "follow_profile": [],
        "repost_post": [],
        "reply_post": [],
    }

    # 1. Due original posts and replies from content queue.
    for item in (content_queue or []):
        if item.get("status") != "queued":
            continue
        sched = item.get("scheduled_after_epoch")
        if sched is not None and float(sched) > now:
            continue
        kind = item.get("kind")
        if kind == "original_post" and len(buckets["publish_post"]) < capped["publish_post"]:
            if stage != "growth" and _EXTERNAL_URL_RE.search(str(item.get("text") or "")):
                continue
            buckets["publish_post"].append({
                "action": "publish_post",
                "text": item["text"],
                "queue_item_id": item.get("item_id"),
                "topic": item.get("topic"),
            })
        elif kind == "reply" and len(buckets["reply_post"]) < capped["reply_post"] and item.get("target_url"):
            match = _STATUS_URL_RE.match(str(item["target_url"]).strip())
            if match:
                canonical = f"https://x.com/{match.group(1).casefold()}/status/{match.group(2)}"
                if canonical not in seen_urls:
                    seen_urls.add(canonical)
                    buckets["reply_post"].append({
                        "action": "reply_post",
                        "url": canonical,
                        "author": match.group(1),
                        "text": item["text"],
                        "queue_item_id": item.get("item_id"),
                    })

    # 2. Deduplicated & topic-ranked timeline/search posts for likes and reposts.
    clean_posts: list[dict[str, Any]] = []
    local_seen_urls = set(seen_urls)
    for post in (timeline_posts or []):
        if not isinstance(post, dict):
            continue
        allowed, rel_score = score_post_relevance(
            post,
            persona_topics=persona_topics,
            exclude_keywords=exclude_keywords,
        )
        if not allowed:
            continue
        raw_url = str(post.get("url") or post.get("post_url") or "").strip()
        match = _STATUS_URL_RE.match(raw_url)
        if not match:
            continue
        canonical = f"https://x.com/{match.group(1).casefold()}/status/{match.group(2)}"
        if canonical in local_seen_urls:
            continue
        local_seen_urls.add(canonical)
        clean_posts.append({
            "url": canonical,
            "author": str(post.get("author") or match.group(1)).lstrip("@"),
            "text_preview": str(post.get("text") or "")[:100],
            "relevance_score": rel_score,
        })
    clean_posts.sort(key=lambda item: item["relevance_score"], reverse=True)

    for post in clean_posts:
        if len(buckets["like_post"]) < capped["like_post"]:
            buckets["like_post"].append({
                "action": "like_post",
                "url": post["url"],
                "author": post["author"],
                "text_preview": post["text_preview"],
            })
        elif len(buckets["repost_post"]) < capped["repost_post"]:
            buckets["repost_post"].append({
                "action": "repost_post",
                "url": post["url"],
                "author": post["author"],
                "text_preview": post["text_preview"],
            })

    # 3. Deduplicated & scored follow candidates (prioritize reciprocal/active accounts).
    ranked_cands = sorted(
        [c for c in (follow_candidates or []) if isinstance(c, dict)],
        key=score_follow_candidate,
        reverse=True,
    )
    local_seen_handles = set(seen_handles)
    for cand in ranked_cands:
        if len(buckets["follow_profile"]) >= capped["follow_profile"]:
            break
        handle = str(cand.get("handle") or cand.get("username") or "").strip().lstrip("@")
        if not _HANDLE_RE.match(handle):
            continue
        key = handle.casefold()
        if key in local_seen_handles:
            continue
        local_seen_handles.add(key)
        score, _ = score_follow_candidate(cand)
        buckets["follow_profile"].append({
            "action": "follow_profile",
            "handle": handle,
            "profile_url": f"https://x.com/{handle}",
            "candidate_score": score,
            "source": str(cand.get("source") or "follow_candidates"),
        })

    # Interleave operations so writes look natural rather than bursting one kind.
    ordered: list[dict[str, Any]] = []
    if buckets["publish_post"]:
        ordered.append(buckets["publish_post"].pop(0))
    order_cycle = ("like_post", "reply_post", "follow_profile", "repost_post", "publish_post")
    while any(buckets.values()):
        for kind in order_cycle:
            if buckets[kind]:
                ordered.append(buckets[kind].pop(0))

    operations: list[dict[str, Any]] = []
    total_delay = 0
    for idx, op in enumerate(ordered):
        cfg = prof["actions"][op["action"]]
        min_gap = int(cfg.get("min_gap_seconds", 60))
        max_gap = max(min_gap, int(cfg.get("max_gap_seconds", min_gap)))
        delay = 0 if idx == 0 else rng.randint(min_gap, max_gap)
        total_delay += delay
        operations.append({
            "step_index": idx + 1,
            **op,
            "delay_before_seconds": delay,
        })

    counts = {
        kind: sum(1 for op in operations if op["action"] == kind)
        for kind in ("publish_post", "like_post", "follow_profile", "repost_post", "reply_post")
    }
    return {
        "status": "planned_not_executed",
        "blocked": False,
        "stage": stage,
        "plan_id": "daily_" + secrets.token_hex(6),
        "health": health,
        "counts": counts,
        "operation_count": len(operations),
        "operations": operations,
        "estimated_duration_seconds": total_delay + len(operations) * 10,
    }
