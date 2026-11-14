"""Detect an account-level suspension from X's own refusal codes.

X answers a write from a suspended account with a permission error instead of a
page failure. The two codes seen in practice are:

    141  "Authorization: User (uid: N) is suspended, deactivated or offboarded"
     37  "Authorization: Denied by access control: Missing TwitterUserNotSuspended"

Both are HTTP 200 responses carrying an errors array, so a DOM-only check reads
them as "the write did not prove itself" and the driver keeps spending actions on
an account that cannot accept any. This module classifies them explicitly.

Usage as a library:
    from account_state import classify_refusal, is_suspended

Usage as a tool:
    python -X utf8 tools/account_state.py --state-dir <dir>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Codes whose message names the account rather than the request.
SUSPENSION_CODES = {141, 37}

SUSPENSION_MARKERS = (
    "suspended",
    "deactivated",
    "offboarded",
    "missing twitterusernotsuspended",
)

# Codes that name a request-level policy block: the account may still write
# something else, so these must not be reported as a suspension.
POLICY_CODES = {283}

ALLOWANCE_CODES = {161}


def classify_refusal(rejection: dict[str, Any] | None) -> str:
    """Return 'suspended', 'policy', 'allowance' or 'unknown' for one refusal."""
    if not isinstance(rejection, dict):
        return "unknown"

    raw_code = rejection.get("code")
    try:
        code = int(str(raw_code).strip())
    except (TypeError, ValueError):
        code = None

    message = str(rejection.get("message") or "").casefold()
    if code in SUSPENSION_CODES or any(m in message for m in SUSPENSION_MARKERS):
        return "suspended"
    if code in ALLOWANCE_CODES:
        return "allowance"
    if code in POLICY_CODES:
        return "policy"
    return "unknown"


def is_suspended(rejection: dict[str, Any] | None) -> bool:
    return classify_refusal(rejection) == "suspended"


def scan_state(state_dir: Path) -> dict[str, Any]:
    """Report every account-level refusal recorded in a ledger."""
    state_path = state_dir / "state.json"
    if not state_path.is_file():
        return {"ok": False, "error": "STATE_NOT_FOUND"}

    ledger = json.loads(state_path.read_text(encoding="utf-8"))
    found: list[dict[str, Any]] = []
    verdicts: dict[str, int] = {}

    for action in ledger.get("actions", {}).values():
        error = action.get("error") or {}
        detail = error.get("details") or {}
        rejection = detail.get("rejection")
        if not isinstance(rejection, dict):
            continue
        verdict = classify_refusal(rejection)
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
        if verdict == "suspended":
            found.append({
                "action_id": action.get("action_id"),
                "type": action.get("type"),
                "at": action.get("completed_at"),
                "code": rejection.get("code"),
                "message": str(rejection.get("message") or "")[:160],
            })

    found.sort(key=lambda row: str(row.get("at")))
    return {
        "ok": True,
        "suspended_refusals": len(found),
        "suspended": bool(found),
        "first_at": found[0]["at"] if found else None,
        "last_at": found[-1]["at"] if found else None,
        "codes": sorted({str(row["code"]) for row in found}),
        "verdicts": verdicts,
        "evidence": found[:10],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args()
    report = scan_state(Path(args.state_dir).expanduser().resolve())
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
