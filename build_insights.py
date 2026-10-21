"""Reduce the raw Bluesky study into a small file the composer can ship with.

The source study measured 43,718 Bluesky posts from 239 accounts. It is not X
data. The reduced file keeps only counts and effect sizes, never post text, so
it can be loaded by the composer without shipping the raw corpus.

Usage:  python -X utf8 build_insights.py
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent / "research" / "06_data" / "bsky_insights.json"
TARGET = ROOT / "data" / "post_insights.json"

SCHEMA = "x-starter-post-insights/v1"

# The study uses Chinese bucket names. Aliases keep consumer code pure ASCII and
# make the file easier to search without touching the source labels.
ALIASES: dict[str, str] = {
    "\u7eaf\u6587\u672c": "text",
    "\u5e26\u94fe\u63a5": "link",
    "\u5e26\u56fe\u7247": "image",
    "\u5e26\u89c6\u9891": "video",
    "\u5e26\u5f15\u7528": "quote",
    "0-80": "b0_80",
    "81-160": "b81_160",
    "161-240": "b161_240",
    "241-300": "b241_300",
}


class BuildError(RuntimeError):
    """Raised when the source study is missing or malformed."""


def _require_list(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key)
    if not isinstance(value, list) or not value:
        raise BuildError(f"{key} is missing or empty in the source study")
    rows: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, dict):
            raise BuildError(f"{key} contains a non-object entry")
        rows.append(row)
    return rows


def _trim_group(row: dict[str, Any]) -> dict[str, Any]:
    key = row.get("key")
    if key is None:
        raise BuildError("a group entry has no key")
    ci = row.get("lift_median_ci95") or []
    if not isinstance(ci, list):
        ci = []
    alias = ALIASES.get(key, key if isinstance(key, str) and key.isascii() else None)
    return {
        "key": key,
        # `feature` mirrors `key` so lookups can use either name.
        "feature": key,
        "alias": alias,
        "n": int(row.get("n", 0)),
        "lift_median": float(row.get("lift_median", 0.0)),
        "lift_median_ci95": [float(item) for item in ci][:2],
    }


def _trim_diff(row: dict[str, Any]) -> dict[str, Any]:
    feature = row.get("feature")
    if feature is None:
        raise BuildError("a top-decile entry has no feature name")
    alias = ALIASES.get(feature, feature if isinstance(feature, str) and feature.isascii() else None)
    return {
        "feature": feature,
        "key": feature,
        "alias": alias,
        "top_n": int(row.get("top_n", 0)),
        "top_share_pct": float(row.get("top_share_pct", 0.0)),
        "rest_n": int(row.get("rest_n", 0)),
        "rest_share_pct": float(row.get("rest_share_pct", 0.0)),
        "difference_pp": float(row.get("difference_pp", 0.0)),
    }


def build(source: Path = SOURCE) -> dict[str, Any]:
    if not source.exists():
        raise BuildError(f"source study not found: {source}")
    with source.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise BuildError("source study must be a JSON object")

    method = raw.get("method") or {}
    summary = raw.get("summary") or {}
    top = raw.get("top_decile_vs_rest") or {}
    if not isinstance(method, dict) or not isinstance(summary, dict) or not isinstance(top, dict):
        raise BuildError("method, summary, and top_decile_vs_rest must be objects")

    collected = str(
        method.get("collection_time_utc") or summary.get("collection_time_utc") or ""
    )
    return {
        "schema": SCHEMA,
        "source": {
            "platform": "Bluesky public API",
            "not_x_data": True,
            "notice": "Bluesky data, not X. Treat every effect size as a weak prior.",
            "date": collected[:10] if collected else "unknown",
            "collection_time_utc": collected,
            "post_count": int(summary.get("post_count_age_ge_48h", 0)),
            "post_count_collected": int(summary.get("post_count_collected", 0)),
            "account_count": int(summary.get("account_count_with_posts", 0)),
            "engagement_definition": str(method.get("engagement") or ""),
            "lift_definition": str(method.get("lift") or ""),
            "bootstrap": str(method.get("bootstrap") or ""),
            "media_rule": str(summary.get("media_category_rule") or ""),
            "length_rule": str(summary.get("length_bucket_rule") or ""),
            "calibration": (
                "Correlation only. Most groups have a median lift of 1.0, so "
                "differences are small and do not prove causation."
            ),
        },
        "by_media": [_trim_group(row) for row in _require_list(raw, "by_media")],
        "by_length_bucket": [
            _trim_group(row) for row in _require_list(raw, "by_length_bucket")
        ],
        "by_hook_type": [
            _trim_group(row) for row in _require_list(raw, "by_hook_type")
        ],
        "top_decile_vs_rest": {
            "top_n": int(top.get("top_n", 0)),
            "rest_n": int(top.get("rest_n", 0)),
            "threshold_lift": float(top.get("threshold_lift", 0.0)),
            "media": [_trim_diff(row) for row in _require_list(top, "media")],
            "length_bucket": [
                _trim_diff(row) for row in _require_list(top, "length_bucket")
            ],
            "hook_type": [_trim_diff(row) for row in _require_list(top, "hook_type")],
            "boolean_features": [
                _trim_diff(row) for row in _require_list(top, "boolean_features")
            ],
        },
    }


def write(payload: dict[str, Any], target: Path = TARGET) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        dir=str(target.parent),
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    return target


def main() -> int:
    payload = build()
    written = write(payload)
    source = payload["source"]
    print(
        f"wrote {written} "
        f"(posts={source['post_count']}, accounts={source['account_count']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
