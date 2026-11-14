"""Rebuild a target pool from the ledger's own record.

The ledger stores every target an action was created for, which makes it the
authoritative list of what has been attempted. When a pool file is lost,
overwritten by a failed harvest, or simply out of date, this tool reconstructs
it: finished targets are reported, unfinished ones are written out as a pool the
driver can consume.

Usage:
    python -X utf8 tools/rebuild_pool.py \
        --state-dir <dir> \
        --action-type repost_post \
        --out <pool.json> \
        [--include-finished]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


FINISHED = {"completed", "failed", "uncertain"}
SUCCESS_STATE = {
    "repost_post": "reposted",
    "like_post": "liked",
    "follow_profile": "following",
}


def rebuild(
    state_dir: Path,
    *,
    action_type: str,
    include_finished: bool = False,
) -> dict[str, object]:
    state_path = state_dir / "state.json"
    if not state_path.is_file():
        return {"ok": False, "error": "STATE_NOT_FOUND", "dir": str(state_dir)}

    ledger = json.loads(state_path.read_text(encoding="utf-8"))
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    succeeded = 0
    finished = 0

    for action in ledger.get("actions", {}).values():
        if action.get("type") != action_type:
            continue
        payload = action.get("payload") or {}
        if action_type == "follow_profile":
            key = str(payload.get("handle", "")).strip()
            url = str(payload.get("profile_url") or ("https://x.com/" + key if key else ""))
        else:
            key = str(payload.get("post_url") or payload.get("url") or "").strip()
            url = key
        if not key or key in seen:
            continue
        seen.add(key)

        result = action.get("result") or {}
        is_success = result.get("state") == SUCCESS_STATE.get(action_type)
        if is_success:
            succeeded += 1
        is_finished = action.get("status") in FINISHED
        if is_finished:
            finished += 1
        if is_finished and not include_finished:
            continue

        entry = {
            "url" if action_type != "follow_profile" else "profile_url": url,
            "author" if action_type != "follow_profile" else "handle":
                str(payload.get("author") or payload.get("handle") or ""),
            "text": "",
        }
        rows.append(entry)

    return {
        "ok": True,
        "action_type": action_type,
        "distinct_targets": len(seen),
        "finished": finished,
        "succeeded": succeeded,
        "written": len(rows),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--action-type", required=True,
                        choices=["repost_post", "like_post", "follow_profile"])
    parser.add_argument("--out", required=True)
    parser.add_argument("--include-finished", action="store_true")
    args = parser.parse_args()

    result = rebuild(
        Path(args.state_dir).expanduser().resolve(),
        action_type=args.action_type,
        include_finished=args.include_finished,
    )
    if not result.get("ok"):
        print(json.dumps(result, ensure_ascii=False))
        return 2

    rows = result.pop("rows")
    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    key = "targets" if args.action_type == "follow_profile" else "posts"
    out.write_text(json.dumps({key: rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    result["out"] = str(out)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
