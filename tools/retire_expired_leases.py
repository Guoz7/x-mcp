"""Retire action leases that expired without a result.

The bridge converts an expired claim to ``uncertain`` only when a worker asks
for work in that same batch. A batch that is never resumed leaves its claims
stuck forever, so the ledger shows work in flight that nobody is running and
the "action in progress" count never settles.

This tool walks the recorded actions, finds claims whose lease has expired, and
moves each to ``uncertain`` with the same code the bridge would use. It writes
through the bridge's own store so the evidence chain stays intact, and it never
touches a live lease.

Usage:
    python -X utf8 tools/retire_expired_leases.py --state-dir <dir> [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from browser_bridge import BridgeError, BrowserBridge  # noqa: E402

EXPIRED_CODE = "ACTION_LEASE_EXPIRED"
EXPIRED_MESSAGE = "The action lease expired. Do not retry silently."


def retire(state_dir: Path, *, dry_run: bool = False) -> dict[str, object]:
    bridge = BrowserBridge(state_dir)
    with bridge._lock:  # noqa: SLF001 - the tool owns the ledger exclusively
        data = bridge._read()  # noqa: SLF001
        now = bridge._now()  # noqa: SLF001
        retired: list[str] = []
        live: list[str] = []

        for action_id, action in data.get("actions", {}).items():
            if action.get("status") != "claimed":
                continue
            lease = action.get("lease_expires_epoch")
            if not isinstance(lease, (int, float)):
                live.append(action_id)
                continue
            if lease >= now:
                live.append(action_id)
                continue
            action["status"] = "uncertain"
            action["error"] = {"code": EXPIRED_CODE, "message": EXPIRED_MESSAGE}
            action["completed_at"] = action.get("completed_at") or _iso(now)
            bridge._mark_write_from_action(  # noqa: SLF001
                data, action, state="uncertain", result=action.get("result")
            )
            bridge._append_evidence(  # noqa: SLF001
                data, "action_lease_retired", action_id,
                {"reason": "lease_expired", "type": action.get("type")},
            )
            retired.append(action_id)

        if retired and not dry_run:
            bridge._write(data)  # noqa: SLF001

        return {
            "retired": len(retired),
            "live": len(live),
            "retired_ids": retired[:20],
            "dry_run": dry_run,
        }


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    directory = Path(args.state_dir).expanduser().resolve()
    if not (directory / "state.json").is_file():
        print(json.dumps({"ok": False, "error": "STATE_NOT_FOUND", "dir": str(directory)}))
        return 2

    try:
        result = retire(directory, dry_run=args.dry_run)
    except BridgeError as exc:
        print(json.dumps({"ok": False, "error": exc.code, "message": exc.message}))
        return 1

    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
