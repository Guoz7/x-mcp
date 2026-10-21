from __future__ import annotations
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from server import XStarter
class ConnectorToolTest(unittest.TestCase):
    def setUp(self): self.app=XStarter(mode="mock", environ={})
    def test_connector_list_has_x_and_fallbacks(self):
        r=self.app.call_tool("connector_list"); self.assertTrue(r["ok"])
        ids={x["connector_id"] for x in r["data"]["connectors"]}
        self.assertTrue({"x-native","x-official-hosted","web-intent","computer-use"} <= ids)
    def test_negotiate_defaults_and_browser(self):
        r=self.app.call_tool("connector_negotiate", {"client_capabilities":{"computer_use":True,"mcp_apps":True}})
        self.assertTrue(r["ok"]); self.assertIn("browser",r["data"]["execution_modes"]); self.assertTrue(r["data"]["mcp_apps"])
    def test_plan_write_requires_confirmation(self):
        r=self.app.call_tool("connector_plan", {"operation":"reply","client_capabilities":{"web_intent":True}})
        self.assertTrue(r["ok"]); self.assertTrue(r["data"]["requires_confirmation"])
        self.assertIn(r["data"]["execution_mode"], {"mcp", "api", "browser", "web_intent"})
        self.assertIn("draft_only", r["data"]["fallback_modes"] + ["draft_only"])
    def test_read_plan_uses_native_connector(self):
        r=self.app.call_tool("connector_plan", {"operation":"read_timeline","client_capabilities":{"api":True}})
        self.assertTrue(r["ok"]); self.assertEqual(r["data"]["execution_mode"],"mcp")
        self.assertEqual(r["data"]["connector_id"],"x-native")
        self.assertFalse(r["data"]["requires_confirmation"])

    def test_no_capable_connector_falls_back_to_draft(self):
        r=self.app.call_tool("connector_plan", {"operation":"book_meeting","client_capabilities":{"api":False,"web_intent":False}})
        self.assertTrue(r["ok"]); self.assertEqual(r["data"]["execution_mode"],"draft_only")
if __name__ == "__main__": unittest.main()
