"""Local Agent Help request board provider and MCP tool tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_help_board import AgentHelpBoard, BoardError  # noqa: E402
from server import XStarter  # noqa: E402


class AgentHelpBoardProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.board = AgentHelpBoard(Path(self.temp.name) / "board.json")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_prepare_publish_list_get(self) -> None:
        prepared = self.board.prepare(
            {
                "title": "Verify browser viewport support",
                "body": "Run read-only checks on a desktop viewport.",
                "tags": ["X", "browser"],
                "required_capabilities": ["computer_use"],
                "acceptance": ["structured result", "no write"],
                "author_agent": "agent-a",
            }
        )
        self.assertEqual(prepared["state"], "prepared")
        request = prepared["request"]
        published = self.board.publish(request["request_id"], prepared["confirmation_phrase"])
        self.assertEqual(published["state"], "open")
        listed = self.board.list(tag="browser")
        self.assertEqual(len(listed["requests"]), 1)
        fetched = self.board.get(request["request_id"])
        self.assertEqual(fetched["request"]["title"], "Verify browser viewport support")
        self.assertEqual(fetched["request"]["status"], "open")

    def test_wrong_confirmation_does_not_publish(self) -> None:
        prepared = self.board.prepare({"title": "Title", "body": "Body"})
        with self.assertRaises(BoardError) as caught:
            self.board.publish(prepared["request"]["request_id"], "wrong")
        self.assertEqual(caught.exception.code, "CONFIRMATION_MISMATCH")
        self.assertEqual(self.board.list()["requests"], [])

    def test_public_request_does_not_accept_credentials(self) -> None:
        with self.assertRaises(BoardError):
            self.board.prepare({"title": "bad", "body": "bad", "token": "secret"})


class AgentHelpBoardMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.app = XStarter(
            mode="mock",
            environ={"X_STARTER_STATE_DIR": self.temp.name},
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_mcp_prepare_publish_and_discover(self) -> None:
        prepared = self.app.call_tool(
            "agent_help_request_publish_prepare",
            {
                "title": "Need a browser compatibility check",
                "body": "Please test the desktop layout.",
                "tags": ["browser"],
                "required_capabilities": ["computer_use"],
                "acceptance": ["return structured evidence"],
                "author_agent": "agent-main",
            },
        )
        self.assertTrue(prepared["ok"], prepared)
        data = prepared["data"]
        request_id = data["request"]["request_id"]
        published = self.app.call_tool(
            "agent_help_request_publish",
            {"request_id": request_id, "confirmation": data["confirmation_phrase"]},
        )
        self.assertTrue(published["ok"], published)
        listed = self.app.call_tool("agent_help_request_list", {"capability": "computer_use"})
        self.assertTrue(listed["ok"], listed)
        self.assertEqual(len(listed["data"]["requests"]), 1)
        fetched = self.app.call_tool("agent_help_request_get", {"request_id": request_id})
        self.assertTrue(fetched["ok"], fetched)
        self.assertEqual(fetched["data"]["request"]["request_id"], request_id)

    def test_tools_are_publicly_discoverable(self) -> None:
        tools = {tool["name"]: tool for tool in self.app.tool_definitions()}
        self.assertEqual(
            tools["agent_help_request_list"]["securitySchemes"],
            [{"type": "noauth"}],
        )
        self.assertNotIn("securitySchemes", tools["agent_help_request_publish"])


if __name__ == "__main__":
    unittest.main()
