"""Fail-closed tests for account snapshots crossing profile boundaries."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from browser_bridge import BrowserBridge, BridgeError  # noqa: E402
from server import XStarter  # noqa: E402


def _owner(owner: str) -> dict:
    return {"X_STARTER_ENABLE_BROWSER": "1", "X_STARTER_OWNER_PROFILE": owner}


class AccountBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.owner_a = self.root / "owner-a"
        self.owner_b = self.root / "owner-b"
        self.owner_a.mkdir()
        self.owner_b.mkdir()
        self.state = self.root / "state"

    def test_snapshot_from_another_profile_cannot_authorize_a_write(self) -> None:
        """This is the fail-open that let a leftover test account act as owner."""

        first = XStarter(
            mode="browser",
            environ={**_owner(str(self.owner_a)), "X_STARTER_STATE_DIR": str(self.state)},
        )
        first._bridge().record_account(
            username="owner", user_id="1", display_name="Owner",
            posts_count=3, recent_post_ids=[],
        )
        prepared = first.call_tool("x_post_prepare", {"text": "hello"})
        self.assertTrue(prepared["ok"], prepared.get("error"))
        first.close()

        # A different profile pointed at the same state must not inherit it.
        second = XStarter(
            mode="browser",
            environ={**_owner(str(self.owner_b)), "X_STARTER_STATE_DIR": str(self.state)},
        )
        try:
            self.assertIsNone(second._bridge().account_snapshot())
            blocked = second.call_tool("x_post_prepare", {"text": "hello"})
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["error"]["code"], "ACCOUNT_OBSERVATION_REQUIRED")
        finally:
            second.close()

    def test_snapshot_is_reused_for_the_same_profile(self) -> None:
        first = XStarter(
            mode="browser",
            environ={**_owner(str(self.owner_a)), "X_STARTER_STATE_DIR": str(self.state)},
        )
        first._bridge().record_account(
            username="owner", user_id="1", display_name="Owner",
            posts_count=3, recent_post_ids=[],
        )
        first.close()
        again = XStarter(
            mode="browser",
            environ={**_owner(str(self.owner_a)), "X_STARTER_STATE_DIR": str(self.state)},
        )
        try:
            snapshot = again._bridge().account_snapshot()
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot["username"], "owner")
        finally:
            again.close()

    def test_no_owner_profile_cannot_borrow_a_stored_snapshot(self) -> None:
        first = XStarter(
            mode="browser",
            environ={**_owner(str(self.owner_a)), "X_STARTER_STATE_DIR": str(self.state)},
        )
        first._bridge().record_account(
            username="owner", user_id="1", display_name="Owner",
            posts_count=3, recent_post_ids=[],
        )
        first.close()
        bare = XStarter(
            mode="browser",
            environ={
                "X_STARTER_ENABLE_BROWSER": "1",
                "X_STARTER_STATE_DIR": str(self.state),
            },
        )
        try:
            self.assertIsNone(bare._bridge().account_snapshot())
        finally:
            bare.close()

    def test_binding_stores_a_hash_not_the_profile_path(self) -> None:
        app = XStarter(
            mode="browser",
            environ={**_owner(str(self.owner_a)), "X_STARTER_STATE_DIR": str(self.state)},
        )
        try:
            app._bridge().record_account(
                username="owner", user_id="1", display_name="Owner",
                posts_count=3, recent_post_ids=[],
            )
            raw = (self.state / "state.json").read_text(encoding="utf-8")
            self.assertNotIn(str(self.owner_a).lower(), raw.lower())
            self.assertIn("account_binding", raw)
        finally:
            app.close()

    def test_bridge_without_binding_rejects_a_stored_snapshot(self) -> None:
        bound = BrowserBridge(self.state, account_binding="owner:abc")
        bound.record_account(
            username="owner", user_id="1", display_name="Owner",
            posts_count=3, recent_post_ids=[],
        )
        self.assertIsNotNone(bound.account_snapshot())
        unbound = BrowserBridge(self.state)
        self.assertIsNone(unbound.account_snapshot())
        with self.assertRaises(BridgeError) as caught:
            unbound.prepare_publish(
                text="hello", expected_username=None, marker=None,
                ttl_seconds=180,
            )
        self.assertEqual(caught.exception.code, "ACCOUNT_OBSERVATION_REQUIRED")


if __name__ == "__main__":
    unittest.main()
