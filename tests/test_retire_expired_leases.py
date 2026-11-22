"""An expired claim must be retired; a live claim must not be touched."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from browser_bridge import BrowserBridge  # noqa: E402
from retire_expired_leases import retire  # noqa: E402


def _seed(directory: str, lease_offset: float) -> tuple[str, str]:
    """Create one claimed action; lease_offset is seconds from now."""
    bridge = BrowserBridge(directory, account_binding="owner:test")
    with bridge._lock:  # noqa: SLF001
        data = bridge._read()  # noqa: SLF001
        action = bridge._new_action(  # noqa: SLF001
            data,
            action_type="like_post",
            payload={"post_url": "https://x.com/a/status/1000", "author": "a"},
            session_id="s1",
        )
        bridge._write(data)  # noqa: SLF001
        action_id = action["action_id"]

    claimed = bridge.next_action(lease_seconds=120, session_id="s1")
    with bridge._lock:  # noqa: SLF001
        data = bridge._read()  # noqa: SLF001
        data["actions"][action_id]["lease_expires_epoch"] = time.time() + lease_offset
        bridge._write(data)  # noqa: SLF001
    return action_id, claimed["claim_token"]


class RetireExpiredLeasesTest(unittest.TestCase):
    def test_an_expired_claim_becomes_uncertain(self):
        with tempfile.TemporaryDirectory() as tmp:
            action_id, _ = _seed(tmp, lease_offset=-60)
            result = retire(Path(tmp))
            self.assertEqual(result["retired"], 1)
            state = json.loads((Path(tmp) / "state.json").read_text(encoding="utf-8"))
            row = state["actions"][action_id]
            self.assertEqual(row["status"], "uncertain")
            self.assertEqual(row["error"]["code"], "ACTION_LEASE_EXPIRED")

    def test_a_live_claim_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            action_id, _ = _seed(tmp, lease_offset=600)
            result = retire(Path(tmp))
            self.assertEqual(result["retired"], 0)
            self.assertEqual(result["live"], 1)
            state = json.loads((Path(tmp) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["actions"][action_id]["status"], "claimed")

    def test_a_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            action_id, _ = _seed(tmp, lease_offset=-60)
            result = retire(Path(tmp), dry_run=True)
            self.assertEqual(result["retired"], 1)
            state = json.loads((Path(tmp) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["actions"][action_id]["status"], "claimed")

    def test_the_evidence_chain_still_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed(tmp, lease_offset=-60)
            retire(Path(tmp))
            state = json.loads((Path(tmp) / "state.json").read_text(encoding="utf-8"))
            types = [row.get("type") for row in state.get("evidence", [])]
            self.assertIn("action_lease_retired", types)
            # The chain is a hash chain; a broken link would make this false.
            prev = None
            for row in state["evidence"]:
                if prev is not None:
                    self.assertEqual(row["prev_hash"], prev)
                prev = row["hash"]


if __name__ == "__main__":
    unittest.main()
