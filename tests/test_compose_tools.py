"""Zero-setup compose tools and the MCP Apps workbench resource."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import WORKBENCH_URI, MCP_APP_MIME, XStarter, handle_message  # noqa: E402


def _rpc(app: XStarter, method: str, params: dict | None = None, rid: int = 1) -> dict:
    return handle_message(app, {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})


class ComposeToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = XStarter(mode="mock", environ={})

    def test_initialize_advertises_mcp_apps_extension(self) -> None:
        result = _rpc(self.app, "initialize", {"protocolVersion": "2025-11-25"})["result"]
        ext = result["capabilities"]["extensions"]["io.modelcontextprotocol/ui"]
        self.assertIn(MCP_APP_MIME, ext["mimeTypes"])
        self.assertIn("x_compose_workbench", result["instructions"])

    def test_workbench_tool_links_ui_resource_and_is_noauth(self) -> None:
        tools = {t["name"]: t for t in _rpc(self.app, "tools/list")["result"]["tools"]}
        tool = tools["x_compose_workbench"]
        self.assertEqual(tool["_meta"]["ui"]["resourceUri"], WORKBENCH_URI)
        self.assertEqual(tool["securitySchemes"], [{"type": "noauth"}])
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        self.assertEqual(tools["x_interaction_link"]["securitySchemes"], [{"type": "noauth"}])
        self.assertNotIn("securitySchemes", tools["x_post_create_api"])

    def test_ui_resource_is_listed_and_readable(self) -> None:
        listed = {r["uri"]: r for r in _rpc(self.app, "resources/list")["result"]["resources"]}
        self.assertEqual(listed[WORKBENCH_URI]["mimeType"], MCP_APP_MIME)
        content = _rpc(self.app, "resources/read", {"uri": WORKBENCH_URI})["result"]["contents"][0]
        self.assertEqual(content["mimeType"], MCP_APP_MIME)
        self.assertIn("ui/initialize", content["text"])
        self.assertEqual(content["_meta"]["ui"]["csp"]["connectDomains"], [])

    def test_workbench_returns_agent_draft_first(self) -> None:
        text = "We cut our MCP tool list from 22 tools to 3 for new users. Setup went from an hour to one click."
        result = self.app.call_tool("x_compose_workbench", {"topic": "onboarding", "text": text})
        self.assertTrue(result["ok"], result)
        data = result["data"]
        self.assertEqual(data["drafts"][0]["id"], "agent")
        self.assertEqual(data["drafts"][0]["text"], text)
        self.assertGreaterEqual(len(data["drafts"]), 4)
        for draft in data["drafts"]:
            self.assertTrue(draft["intent_url"].startswith("https://x.com/intent/tweet?"))
        decoded = parse_qs(urlsplit(data["drafts"][0]["intent_url"]).query)["text"][0]
        self.assertEqual(decoded, text)
        self.assertEqual(data["insights_source"]["posts"], 43718)
        self.assertIn("not X", data["insights_source"]["note"])

    def test_workbench_rejects_bad_tone(self) -> None:
        result = self.app.call_tool("x_compose_workbench", {"topic": "x", "tone": "angry"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")

    def test_check_flags_too_long_and_withholds_link(self) -> None:
        result = self.app.call_tool("x_compose_check", {"text": "a" * 281})
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["data"]["valid"])
        self.assertIsNone(result["data"]["intent_url"])
        ok = self.app.call_tool("x_compose_check", {"text": "你好" * 70})
        self.assertTrue(ok["data"]["valid"])
        self.assertEqual(ok["data"]["weighted_length"], 280)

    def test_interaction_links(self) -> None:
        cases = {
            "like": ("https://x.com/intent/like?tweet_id=2112242496441061458", {"post": "https://x.com/a/status/2112242496441061458"}),
            "repost": ("https://x.com/intent/retweet?tweet_id=2112242496441061458", {"post": "2112242496441061458"}),
            "follow": ("https://x.com/intent/follow?screen_name=guoz7777", {"screen_name": "@guoz7777"}),
        }
        for action, (url, extra) in cases.items():
            result = self.app.call_tool("x_interaction_link", {"action": action, **extra})
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["data"]["intent_url"], url)
            self.assertFalse(result["data"]["posts_automatically"])

    def test_interaction_link_rejects_foreign_url(self) -> None:
        result = self.app.call_tool("x_interaction_link", {"action": "like", "post": "https://evil.example/status/1"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_POST_REF")

    def test_compose_tools_make_no_network_calls(self) -> None:
        result = self.app.call_tool("x_compose_workbench", {"topic": "test"})
        trace = json.dumps(result["meta"])
        self.assertNotIn('"external_call": true', trace)
        self.assertIn('"external_call": false', trace)


if __name__ == "__main__":
    unittest.main()
