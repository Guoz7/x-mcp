"""Browser context and locator metadata contract tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from browser_bridge import BrowserBridge, BridgeError  # noqa: E402
from server import XStarter  # noqa: E402


class BrowserCompatibilityContractTests(unittest.TestCase):
    def _claimed_account_action(self) -> tuple[BrowserBridge, dict]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bridge = BrowserBridge(temp.name)
        bridge.queue_observation(action_type="observe_account")
        action = bridge.next_action()
        self.assertIsNotNone(action)
        return bridge, action

    def test_claim_includes_context_contract_and_viewport_classes(self) -> None:
        _bridge, action = self._claimed_account_action()
        self.assertEqual(action["context_contract"]["schema"], "x-starter-browser-context/v1")
        self.assertEqual(action["viewport_classes"], ["mobile", "tablet", "desktop"])
        self.assertEqual(action["context_contract"]["page_kinds"], ["x_profile"])
        self.assertEqual(
            action["context_contract"]["locator_priority"],
            ["accessibility", "dom", "text", "screenshot", "coordinate", "manual"],
        )

    def test_completion_persists_safe_executor_metadata(self) -> None:
        bridge, action = self._claimed_account_action()
        result = bridge.complete_action(
            action_id=action["action_id"],
            claim_token=action["claim_token"],
            outcome="succeeded",
            result={
                "username": "guoz7777",
                "user_id": "1597769294179627010",
                "display_name": "guoz777",
                "posts_count": 5,
                "recent_post_ids": [],
            },
            error=None,
            executor_context={
                "browser_family": "chromium",
                "browser_version": "131",
                "os": "windows",
                "viewport": {"width": 1440, "height": 900},
                "device_pixel_ratio": 1,
                "zoom_percent": 100,
                "locale": "zh-CN",
                "color_scheme": "light",
                "tab": {
                    "id": "tab-1",
                    "url": "https://x.com/home?tracking=secret#fragment",
                    "title": "X",
                },
                "account_hint": "@guoz7777",
            },
            locator_strategy="accessibility",
            preconditions_checked=["current URL is X"],
            postconditions_checked=["visible handle matched"],
        )
        self.assertEqual(result["execution"]["locator_strategy"], "accessibility")
        context = result["execution"]["executor_context"]
        self.assertEqual(context["tab"]["url"], "https://x.com/home")
        self.assertEqual(context["account_hint"], "guoz7777")
        self.assertEqual(
            result["execution"]["postconditions_checked"],
            ["visible handle matched"],
        )

        reread = bridge.get_action(action["action_id"])
        self.assertEqual(reread["execution"], result["execution"])

    def test_sensitive_context_is_rejected(self) -> None:
        bridge, action = self._claimed_account_action()
        with self.assertRaises(BridgeError) as caught:
            bridge.complete_action(
                action_id=action["action_id"],
                claim_token=action["claim_token"],
                outcome="uncertain",
                result=None,
                error={"code": "PAGE_UNCLEAR"},
                executor_context={"cookies": "must-not-enter"},
            )
        self.assertEqual(caught.exception.code, "SENSITIVE_CONTEXT_REJECTED")

    def test_invalid_locator_strategy_is_rejected(self) -> None:
        bridge, action = self._claimed_account_action()
        with self.assertRaises(BridgeError) as caught:
            bridge.complete_action(
                action_id=action["action_id"],
                claim_token=action["claim_token"],
                outcome="uncertain",
                result=None,
                error={"code": "PAGE_UNCLEAR"},
                locator_strategy="pixel_magic",
            )
        self.assertEqual(caught.exception.code, "INVALID_EXECUTION_METADATA")

    def test_browser_family_gate_accepts_firefox_and_rejects_chromium(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bridge = BrowserBridge(temp.name, allowed_browser_family="firefox")
        bridge.queue_observation(action_type="observe_account")
        firefox = bridge.next_action()
        self.assertEqual(firefox["context_contract"]["allowed_browser_families"], ["firefox"])
        with self.assertRaises(BridgeError) as caught:
            bridge.complete_action(
                action_id=firefox["action_id"],
                claim_token=firefox["claim_token"],
                outcome="uncertain",
                result=None,
                error={"code": "CONTROLLED_EXPERIMENT"},
                executor_context={"browser_family": "chromium"},
            )
        self.assertEqual(caught.exception.code, "BROWSER_FAMILY_NOT_ALLOWED")
        completed = bridge.complete_action(
            action_id=firefox["action_id"],
            claim_token=firefox["claim_token"],
            outcome="uncertain",
            result=None,
            error={"code": "CONTROLLED_EXPERIMENT"},
            executor_context={"browser_family": "firefox"},
        )
        self.assertEqual(completed["status"], "uncertain")

    def test_mcp_schema_exposes_metadata_inputs(self) -> None:
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        app = XStarter(
            mode="browser",
            environ={"X_STARTER_ENABLE_BROWSER": "1", "X_STARTER_STATE_DIR": state.name},
        )
        tools = {tool["name"]: tool for tool in app.tool_definitions()}
        schema = tools["x_browser_complete_action"]["inputSchema"]
        properties = schema["properties"]
        for name in (
            "executor_context",
            "locator_strategy",
            "preconditions_checked",
            "postconditions_checked",
        ):
            self.assertIn(name, properties)
        app.call_tool("x_account_get", {"refresh": True})
        claimed = app.call_tool("x_browser_next_action")
        self.assertIn("viewport_classes", claimed["data"]["action"])


if __name__ == "__main__":
    unittest.main()
