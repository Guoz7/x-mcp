"""Record one time-boxed, count-boxed operator window for real follow runs."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_bridge import BrowserBridge  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--account-binding", required=True)
    parser.add_argument("--max-actions", type=int, required=True)
    parser.add_argument("--minutes", type=int, default=360)
    parser.add_argument("--reason", default="operator confirmed no X challenge")
    args = parser.parse_args()
    bridge = BrowserBridge(args.state_dir, account_binding=args.account_binding)
    with bridge._lock:
        data = bridge._read()
        if data.get("account_binding") != args.account_binding:
            raise SystemExit("ACCOUNT_BINDING_MISMATCH")
        now = time.time()
        window = {
            "mode": "bounded_follow_run",
            "account_binding": args.account_binding,
            "authorized_epoch": now,
            "expires_epoch": now + args.minutes * 60,
            "max_actions": args.max_actions,
            "used_actions": 0,
            "reason": args.reason,
        }
        data["operator_recovery"] = window
        bridge._append_evidence(data, "operator_window_authorized", "follow", window)
        bridge._write(data)
    print(json.dumps({"authorized": True, "max_actions": args.max_actions}))


if __name__ == "__main__":
    main()
