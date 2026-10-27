"""Plan and stage a bounded X interaction batch without touching X.

Two executors can run the plan, and both use the same claims and leases:

* the built-in browser executor, one action at a time;
* any external agent, which claims one action, performs it, and reports back.

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


# Per-batch ceilings. A caller that wants more splits the work into several
# confirmed batches; there is no silent truncation.
MAX_LIKES = 200
MAX_REPOSTS = 10
MAX_REPLIES = 200
MAX_OPERATIONS = 410
DEFAULT_CHUNK_SIZE = 20


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
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> dict[str, Any]:
    """Return a deterministic, side-effect-free batch plan."""

    if not isinstance(posts, list):
        raise BatchPlanError("posts must be an array.")
    if (
        not 1 <= like_count <= MAX_LIKES
        or not 0 <= repost_count <= MAX_REPOSTS
        or not 0 <= reply_count <= MAX_REPLIES
    ):
        raise BatchPlanError("Counts exceed the bounded batch limits.")
    if like_count + repost_count + reply_count > MAX_OPERATIONS:
        raise BatchPlanError("The batch exceeds the operation limit.")
    if not 1 <= chunk_size <= 50:
        raise BatchPlanError("chunk_size must be from 1 to 50.")
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
    # Reposts may target selected posts; the likes already require distinct
    # authors, so a repost does not need a 201st target.
    for index in range(repost_count):
        target = like_targets[index % len(like_targets)]
        operations.append(
            {
                "sequence": len(operations) + 1,
                "action": "repost",
                "url": target["url"],
                "author": target["author"],
            }
        )
    reply_base = len(operations)
    reply_targets = [targets[index % len(targets)] for index in range(reply_count)]
    operations.extend(
        {"sequence": reply_base + index + 1, "action": "reply", "url": target["url"], "author": target["author"], "text": reply_texts[index]}
        for index, target in enumerate(reply_targets)
    )
    for index, operation in enumerate(operations):
        operation["chunk"] = index // chunk_size + 1
    chunk_count = (len(operations) + chunk_size - 1) // chunk_size
    canonical = json.dumps(operations, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "status": "planned_not_executed",
        "batch_id": "batch_" + hashlib.sha256(canonical.encode()).hexdigest()[:16],
        "budget_seconds": budget_seconds,
        "operation_count": len(operations),
        "chunk_size": chunk_size,
        "chunk_count": chunk_count,
        "counts": {"like": like_count, "repost": repost_count, "reply": reply_count},
        "distinct_like_authors": len({item["author"].casefold() for item in like_targets}),
        "operations": operations,
        "execution": {
            "supported_by_current_browser_bridge": True,
            "requires_action_executor": True,
            "executor_options": ["builtin_browser", "external_agent"],
            "real_x_state_changed": False,
            "one_minute_sla": "not_verified",
        },
        "disclosure": "Planning only. No X page was opened and no interaction was sent.",
    }
