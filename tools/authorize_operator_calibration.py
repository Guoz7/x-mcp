"""Record one explicit operator-authorized calibration permit."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_bridge import BrowserBridge, BridgeError


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--state-dir", required=True)
    p.add_argument("--account-binding", required=True)
    p.add_argument("--target", required=True)
    args = p.parse_args()
    target = args.target.lstrip("@").casefold()
    bridge = BrowserBridge(args.state_dir, account_binding=args.account_binding)
    with bridge._lock:
        data = bridge._read()
        if data.get("account_binding") != args.account_binding:
            raise BridgeError("ACCOUNT_BINDING_MISMATCH", "Account binding does not match ledger.")
        previous = data.get("operator_recovery")
        if isinstance(previous, dict):
            now_epoch = time.time()
            reusable = (
                previous.get("consumed") is not True
                and isinstance(previous.get("expires_epoch"), (int, float))
                and float(previous["expires_epoch"]) <= now_epoch
            )
            if not reusable:
                raise BridgeError(
                    "RECOVERY_ALREADY_USED",
                    "An unused calibration permit already exists, or a prior one was consumed.",
                )
        for action in data.get("actions", {}).values():
            if str((action.get("payload") or {}).get("handle", "")).casefold() == target:
                raise BridgeError("CALIBRATION_TARGET_USED", "Target was already attempted.")
        now = time.time()
        permit = {
            "allow_one_calibration": True,
            "account_binding": args.account_binding,
            "target_handle": target,
            "authorized_at": now,
            "expires_epoch": now + 1200,
            "reason": "explicit operator confirmation; current X page readback reported no challenge",
        }
        data["operator_recovery"] = permit
        bridge._append_evidence(data, "operator_calibration_authorized", target, permit)
        bridge._write(data)
    print(json.dumps({"authorized": True, "target": target, "max_actions": 1}))


if __name__ == "__main__":
    main()
