"""Plan and stage a bounded X interaction batch without touching X.

Execution remains a separate browser action lease. A trusted executor can use
DOM/accessibility locators first and screenshot coordinates as the fallback.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlparse


class BatchPlanError(ValueError):
    """A caller supplied an invalid or insufficient batch."""


def _status_url(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 300:
        raise BatchPlanError("Each target needs a short status URL.")
    parsed = urlparse(value)
    parts = [item for item in parsed.path.split("/") if item]
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "x.com", "www.x.com", "twitter.com", "www.twitter.com"
    } or len(parts) != 3 or parts[1] != "status" or not parts[2].isdigit():
        raise BatchPlanError("Each target must be a canonical X status URL.")
    return f"https://x.com/{parts[0]}/status/{parts[2]}"


def plan_batch(
    posts: list[dict[str, Any]],
    *,
    reply_texts: list[str],
    like_count: int = 20,
    repost_count: int = 1,
    reply_count: int = 10,
    budget_seconds: int = 60,
) -> dict[str, Any]:
    """Return a deterministic, side-effect-free batch plan."""

    if not isinstance(posts, list):
        raise BatchPlanError("posts must be an array.")
    if not 1 <= like_count <= 20 or not 0 <= repost_count <= 1 or not 0 <= reply_count <= 10:
        raise BatchPlanError("Counts exceed the bounded batch limits.")
    if budget_seconds < 1 or budget_seconds > 600:
        raise BatchPlanError("budget_seconds must be from 1 to 600.")
    if len(reply_texts) != reply_count or any(
        not isinstance(text, str) or not 1 <= len(text) <= 280 for text in reply_texts
    ):
        raise BatchPlanError("reply_texts must contain one 1-280 character draft per reply.")

    targets: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in posts:
        if not isinstance(item, dict):
            raise BatchPlanError("Each post must be an object.")
        author = item.get("author")
        # Keep the public field name stable while accepting the early prototype
        # alias so recorded clients can be replayed without changing targets.
        url = _status_url(item.get("url", item.get("post_url")))
        if not isinstance(author, str) or not 1 <= len(author) <= 80:
            raise BatchPlanError("Each post needs a short author.")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        targets.append({"url": url, "author": author, "text": str(item.get("text", ""))[:500]})

    if len(targets) < max(like_count, reply_count, repost_count):
        raise BatchPlanError("The observed timeline does not contain enough unique posts.")
    like_targets: list[dict[str, str]] = []
    seen_authors: set[str] = set()
    for target in targets:
        if target["author"].casefold() in seen_authors:
            continue
        like_targets.append(target)
        seen_authors.add(target["author"].casefold())
        if len(like_targets) == like_count:
            break
    if len(like_targets) != like_count:
        raise BatchPlanError("Likes require the requested number of distinct authors.")

    operations: list[dict[str, Any]] = [
        {"sequence": index + 1, "action": "like", "url": target["url"], "author": target["author"]}
        for index, target in enumerate(like_targets)
    ]
    if repost_count:
        # The repost may target one of the selected posts. The requirement is
        # 20 distinct like authors, not a 21st author for the repost.
        target = like_targets[0]
        operations.append({"sequence": len(operations) + 1, "action": "repost", "url": target["url"], "author": target["author"]})
    reply_base = len(operations)
    operations.extend(
        {"sequence": reply_base + index + 1, "action": "reply", "url": target["url"], "author": target["author"], "text": reply_texts[index]}
        for index, target in enumerate(targets[:reply_count])
    )
    canonical = json.dumps(operations, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "status": "planned_not_executed",
        "batch_id": "batch_" + hashlib.sha256(canonical.encode()).hexdigest()[:16],
        "budget_seconds": budget_seconds,
        "operation_count": len(operations),
        "counts": {"like": like_count, "repost": repost_count, "reply": reply_count},
        "distinct_like_authors": len({item["author"].casefold() for item in like_targets}),
        "operations": operations,
        "execution": {
            "supported_by_current_browser_bridge": True,
            "requires_action_executor": True,
            "real_x_state_changed": False,
            "one_minute_sla": "not_verified",
        },
        "disclosure": "Planning only. No X page was opened and no interaction was sent.",
    }
