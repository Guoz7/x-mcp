"""Plan a confirmed X follow batch without touching X.

Targets must carry visible evidence that the owner can follow them back. The
browser executor verifies the live profile state again before each click.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlparse


class FollowBatchPlanError(ValueError):
    """A caller supplied an invalid or insufficient follow batch."""


MAX_FOLLOWS = 200
MAX_BATCH_OPERATIONS = 200
DEFAULT_CHUNK_SIZE = 20
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}


def _handle(value: Any) -> str:
    if not isinstance(value, str):
        raise FollowBatchPlanError("Each target needs an X handle.")
    value = value.strip().lstrip("@").strip()
    if not _HANDLE_RE.fullmatch(value):
        raise FollowBatchPlanError("Each target needs a valid X handle.")
    return value


def _profile_url(value: Any, handle: str) -> str:
    if value in (None, ""):
        return f"https://x.com/{handle}"
    if not isinstance(value, str) or len(value) > 240:
        raise FollowBatchPlanError("profile_url must be a short X profile URL.")
    parsed = urlparse(value.strip())
    parts = [item for item in parsed.path.split("/") if item]
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in _X_HOSTS
        or len(parts) != 1
        or parts[0].casefold() != handle.casefold()
        or not _HANDLE_RE.fullmatch(parts[0])
    ):
        raise FollowBatchPlanError("profile_url must match the target handle.")
    return f"https://x.com/{parts[0]}"


def plan_batch(
    targets: list[dict[str, Any]],
    *,
    budget_seconds: int = 600,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    require_mutual: bool = True,
) -> dict[str, Any]:
    """Return a deterministic, side-effect-free follow plan."""

    if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_FOLLOWS:
        raise FollowBatchPlanError("targets must contain from 1 to 200 accounts.")
    if not 1 <= chunk_size <= 50:
        raise FollowBatchPlanError("chunk_size must be from 1 to 50.")
    if not 1 <= budget_seconds <= 600:
        raise FollowBatchPlanError("budget_seconds must be from 1 to 600.")
    if not isinstance(require_mutual, bool):
        raise FollowBatchPlanError("require_mutual must be a boolean.")

    operations: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped_already_following = 0
    for item in targets:
        if not isinstance(item, dict):
            raise FollowBatchPlanError("Each target must be an object.")
        handle = _handle(item.get("handle", item.get("screen_name")))
        key = handle.casefold()
        if key in seen:
            continue
        seen.add(key)

        mutual_verified = item.get("mutual_verified", item.get("follows_you", False))
        if not isinstance(mutual_verified, bool):
            raise FollowBatchPlanError("mutual_verified must be a boolean.")
        if require_mutual and not mutual_verified:
            raise FollowBatchPlanError(
                f"Target @{handle} has no visible mutual-follow proof."
            )

        state = str(item.get("state", "follow_back_available")).strip().lower()
        if state in {"already_following", "following"}:
            skipped_already_following += 1
            continue
        if state not in {"follow_back_available", "not_following", "follow_available"}:
            raise FollowBatchPlanError(
                f"Target @{handle} has unsupported state {state!r}."
            )
        operation = {
            "sequence": len(operations) + 1,
            "action": "follow",
            "handle": handle,
            "profile_url": _profile_url(item.get("profile_url"), handle),
            "mutual_verified": mutual_verified,
            "candidate_reason": str(item.get("candidate_reason", "visible_follow_back"))[:240],
        }
        operations.append(operation)
        if len(operations) == MAX_BATCH_OPERATIONS:
            break

    if not operations:
        raise FollowBatchPlanError("No followable targets remain after filtering.")
    for index, operation in enumerate(operations):
        operation["chunk"] = index // chunk_size + 1
    chunk_count = (len(operations) + chunk_size - 1) // chunk_size
    canonical = json.dumps(
        operations,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "status": "planned_not_executed",
        "batch_id": "follow_" + hashlib.sha256(canonical.encode()).hexdigest()[:16],
        "budget_seconds": budget_seconds,
        "operation_count": len(operations),
        "chunk_size": chunk_size,
        "chunk_count": chunk_count,
        "counts": {"follow": len(operations), "skipped_already_following": skipped_already_following},
        "operations": operations,
        "execution": {
            "supported_by_current_browser_bridge": True,
            "requires_action_executor": True,
            "executor_options": ["builtin_browser", "external_agent"],
            "real_x_state_changed": False,
            "ten_minute_sla": "not_verified",
        },
        "disclosure": (
            "Planning only. No X page was opened and no follow was sent. "
            "Each target carries visible mutual-follow evidence."
        ),
    }
