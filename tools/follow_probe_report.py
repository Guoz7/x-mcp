"""Summarise a series of follow-allowance probes.

A single probe says "refused". Only a series says whether the allowance moves,
and what it moves with. Each probe row carries the account counters observed at
that moment, so this report can answer: did the allowance change when the
follower count changed, or did it not change at all?

Usage:
    python -X utf8 tools/follow_probe_report.py --log <probes.jsonl>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def load_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"probes": 0, "verdict": "no_data"}

    outcomes = {}
    follower_series = []
    changed_followers = 0
    previous_followers = None

    for row in rows:
        result = row.get("result") or {}
        code = result.get("rejection_code") or result.get("code") or "unknown"
        outcomes[str(code)] = outcomes.get(str(code), 0) + 1

        followers = row.get("followers")
        follower_series.append(followers)
        if previous_followers is not None and followers != previous_followers:
            changed_followers += 1
        previous_followers = followers

    refused_161 = outcomes.get("161", 0)
    succeeded = sum(
        1 for row in rows
        if (row.get("result") or {}).get("state") == "following"
    )

    # The verdict describes the current state, so the newest probe decides.
    # Counting historical refusals would keep reporting a block after the
    # allowance opened, which is the opposite of useful.
    latest = rows[-1].get("result") or {}
    latest_open = latest.get("state") == "following"

    if latest_open:
        verdict = "allowance_open"
    elif not succeeded and changed_followers == 0:
        verdict = "blocked_and_followers_flat"
    elif not succeeded:
        verdict = "blocked_but_followers_moved"
    else:
        verdict = "mixed"

    return {
        "probes": len(rows),
        "outcomes": outcomes,
        "succeeded": succeeded,
        "refused_161": refused_161,
        "latest_open": latest_open,
        "follower_series": follower_series,
        "follower_changes": changed_followers,
        "first_at": rows[0].get("at"),
        "last_at": rows[-1].get("at"),
        "verdict": verdict,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()

    rows = load_rows(Path(args.log).expanduser().resolve())
    print(json.dumps(summarise(rows), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
