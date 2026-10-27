from __future__ import annotations

import hashlib
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from http.server import ThreadingHTTPServer  # noqa: E402

from server import (  # noqa: E402
    StarterError,
    XStarter,
    config_report,
    handle_message,
    make_http_handler,
)
from oauth import OAuthManager  # noqa: E402


class _FakeTokenResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class MockToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = XStarter(mode="mock", environ={})

    def test_default_mode_is_mock(self) -> None:
        app = XStarter(environ={})
        self.assertEqual(app.mode, "mock")

    def test_tool_list_has_required_tools(self) -> None:
        response = handle_message(
            self.app,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(
            names,
            {
                "x_session_status",
                "x_oauth_start",
                "x_oauth_status",
                "x_oauth_disconnect",
                "x_post_prepare_api",
                "x_post_create_api",
                "x_account_get",
                "x_timeline_observe",
                "x_post_prepare",
                "x_post_publish",
                "x_post_delete",
                "x_timeline_restore",
                "browser_open",
                "browser_observe",
                "browser_screenshot",
                "browser_click",
                "browser_scroll",
                "browser_type",
                "browser_close",
                "x_browser_next_action",
                "x_browser_complete_action",
                "x_browser_action_get",
                "x_owner_read",
                "x_evidence_get",
                "x_interaction_batch_plan",
                "x_interaction_batch_prepare",
                "x_interaction_batch_start",
                "x_interaction_batch_execute",
                "x_interaction_batch_status",
                "x_follow_batch_plan",
                "x_follow_candidates_read",
                "x_follow_batch_prepare",
                "x_follow_batch_start",
                "x_follow_batch_execute",
                "x_follow_batch_status",
                "account_summary",
                "draft_post",
                "schedule_post",
                "recent_metrics",
                "disconnect",
                "ahan_handoff",
                "x_compose_workbench",
                "x_compose_check",
                "x_interaction_link",
                "x_public_profile",
                "x_public_timeline",
                "x_public_post",
                "agent_help_request_publish_prepare",
                "agent_help_request_publish",
                "agent_help_request_list",
                "agent_help_request_get",
                "connector_list",
                "connector_negotiate",
                "connector_plan",
            },
        )
        for tool in response["result"]["tools"]:
            self.assertIn("outputSchema", tool, tool["name"])
            self.assertFalse(tool["outputSchema"]["additionalProperties"])

    def test_mock_four_step_flow_has_trace_layers(self) -> None:
        account = self.app.call_tool("account_summary")
        self.assertTrue(account["ok"])
        self.assertTrue(account["meta"]["simulated"])
        self.assertEqual(account["data"]["username"], "mock_creator")
        layers = {event["layer"] for event in account["meta"]["trace"]["events"]}
        self.assertIn("agent", layers)
        self.assertIn("mcp_server", layers)
        self.assertIn("auth_boundary", layers)
        self.assertIn("x_adapter", layers)

        draft = self.app.call_tool(
            "draft_post",
            {"topic": "one small workflow improvement", "tone": "clear"},
        )
        self.assertTrue(draft["ok"])
        self.assertLessEqual(draft["data"]["character_count"], 280)

        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        scheduled = self.app.call_tool(
            "schedule_post",
            {"content": draft["data"]["content"], "scheduled_for": future},
        )
        self.assertTrue(scheduled["ok"])
        self.assertEqual(scheduled["data"]["status"], "mock_scheduled")

        metrics = self.app.call_tool("recent_metrics", {"days": 7})
        self.assertTrue(metrics["ok"])
        self.assertGreater(metrics["data"]["metrics"]["impressions"], 0)

    def test_ahan_handoff_has_no_x_credentials(self) -> None:
        result = self.app.call_tool("ahan_handoff", {"creator_alias": "demo"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["status"], "not_contacted")
        self.assertFalse(result["data"]["request"]["x_credentials_included"])
        self.assertNotIn("X_BEARER_TOKEN", json.dumps(result))

    def test_disconnect_clears_session(self) -> None:
        self.app.call_tool("account_summary")
        result = self.app.call_tool("disconnect")
        self.assertTrue(result["ok"])
        self.assertFalse(self.app.connected)

    def test_schedule_rejects_past_time(self) -> None:
        result = self.app.call_tool(
            "schedule_post",
            {
                "content": "A valid post",
                "scheduled_for": "2020-01-01T00:00:00Z",
            },
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")

    def test_mock_new_tools_are_available(self) -> None:
        status = self.app.call_tool("x_session_status")
        self.assertTrue(status["ok"], status)
        account = self.app.call_tool("x_account_get")
        self.assertTrue(account["ok"], account)
        timeline = self.app.call_tool("x_timeline_observe", {"limit": 1})
        self.assertTrue(timeline["ok"], timeline)
        self.assertEqual(len(timeline["data"]["posts"]), 1)
        write = self.app.call_tool("x_post_prepare", {"text": "mock"})
        self.assertFalse(write["ok"])
        self.assertEqual(write["error"]["code"], "BROWSER_BRIDGE_REQUIRED")


class LiveConfigTest(unittest.TestCase):
    def test_live_mode_fails_closed_without_config(self) -> None:
        with self.assertRaises(StarterError) as caught:
            XStarter(mode="live", environ={})
        self.assertEqual(caught.exception.code, "LIVE_CONFIG_MISSING")

    def test_live_mode_requires_enable_gate(self) -> None:
        with self.assertRaises(StarterError) as caught:
            XStarter(
                mode="live",
                environ={
                    "X_STARTER_ENABLE_LIVE": "0",
                    "X_BEARER_TOKEN": "not-a-real-token",
                },
            )
        self.assertEqual(caught.exception.code, "LIVE_NOT_ENABLED")

    def test_live_config_does_not_expose_token(self) -> None:
        token = "sentinel-test-token"
        app = XStarter(
            mode="live",
            environ={"X_STARTER_ENABLE_LIVE": "1", "X_BEARER_TOKEN": token},
        )
        self.assertEqual(app.mode, "live")
        self.assertNotIn(token, repr(app))
        self.assertNotIn(token, repr(app.live_config))
        self.assertNotIn(token, json.dumps(config_report(app)))

    def test_oauth_pkce_start_does_not_expose_secrets(self) -> None:
        app = XStarter(
            mode="live",
            environ={
                "X_STARTER_ENABLE_LIVE": "1",
                "X_STARTER_X_CLIENT_ID": "client-public-id",
                "X_STARTER_OAUTH_REDIRECT_URI": "http://127.0.0.1:8765/oauth/callback",
                "X_STARTER_TOKEN_STORE": str(Path(tempfile.gettempdir()) / "x-starter-oauth-test.json"),
            },
        )
        result = app.call_tool("x_oauth_start", {"account_ref": "creator-a"})
        self.assertTrue(result["ok"], result)
        url = result["data"]["authorization_url"]
        self.assertIn("code_challenge=", url)
        self.assertIn("client_id=client-public-id", url)
        self.assertNotIn("access_token", json.dumps(result))
        self.assertNotIn("refresh_token", json.dumps(result))

    def test_oauth_status_is_empty_without_token(self) -> None:
        path = Path(tempfile.gettempdir()) / "x-starter-oauth-empty.json"
        path.unlink(missing_ok=True)
        app = XStarter(
            mode="live",
            environ={
                "X_STARTER_ENABLE_LIVE": "1",
                "X_STARTER_X_CLIENT_ID": "client-public-id",
                "X_STARTER_TOKEN_STORE": str(path),
            },
        )
        result = app.call_tool("x_oauth_status")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["accounts"], [])
        self.assertNotIn("access_token", json.dumps(result))

    def test_oauth_callback_persists_only_metadata_in_public_status(self) -> None:
        path = Path(tempfile.gettempdir()) / "x-starter-oauth-callback.json"
        path.unlink(missing_ok=True)
        clock = [1000.0]
        manager = OAuthManager(
            client_id="client-public-id",
            redirect_uri="http://127.0.0.1:8765/oauth/callback",
            store_path=path,
            now=lambda: clock[0],
        )
        started = manager.start("creator-a")
        query = "?code=demo-code&state=" + started["authorization_url"].split("state=", 1)[1].split("&", 1)[0]
        original = __import__("oauth")
        original.urlopen = lambda request, timeout=20: _FakeTokenResponse(
            {"access_token": "secret-access", "refresh_token": "secret-refresh", "expires_in": 7200, "scope": "tweet.read tweet.write users.read offline.access"}
        )
        result = manager.callback(query)
        self.assertEqual(result["status"], "connected")
        status = manager.status()
        self.assertEqual(status["accounts"][0]["account_ref"], "creator-a")
        self.assertNotIn("secret-access", json.dumps(status))
        self.assertNotIn("secret-refresh", json.dumps(status))

    def test_oauth_access_token_refreshes_expired_record(self) -> None:
        path = Path(tempfile.gettempdir()) / "x-starter-oauth-refresh.json"
        path.unlink(missing_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": "x-starter-oauth/v1",
                    "accounts": {
                        "creator-a": {
                            "access_token": "expired",
                            "refresh_token": "refresh-secret",
                            "expires_at": 900,
                            "scope": "tweet.read tweet.write users.read offline.access",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        manager = OAuthManager(
            client_id="client-public-id",
            redirect_uri="http://127.0.0.1:8765/oauth/callback",
            store_path=path,
            now=lambda: 1000.0,
        )
        original = __import__("oauth")
        original.urlopen = lambda request, timeout=20: _FakeTokenResponse(
            {"access_token": "fresh-access", "refresh_token": "fresh-refresh", "expires_in": 7200}
        )
        self.assertEqual(manager.access_token("creator-a"), "fresh-access")

    def test_api_write_fails_closed_without_oauth_user_context(self) -> None:
        path = Path(tempfile.gettempdir()) / "x-starter-oauth-write.json"
        path.unlink(missing_ok=True)
        app = XStarter(
            mode="live",
            environ={
                "X_STARTER_ENABLE_LIVE": "1",
                "X_STARTER_X_CLIENT_ID": "client-public-id",
                "X_STARTER_TOKEN_STORE": str(path),
            },
        )
        prepared = app.call_tool(
            "x_post_prepare_api",
            {"text": "API write test"},
            session_id="session-a",
        )
        self.assertTrue(prepared["ok"], prepared)
        result = app.call_tool(
            "x_post_create_api",
            {
                "write_id": prepared["data"]["write_id"],
                "confirmation": prepared["data"]["confirmation_phrase"],
            },
            session_id="session-a",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "X_AUTH_REQUIRED")

    def test_live_schedule_is_rejected_before_network(self) -> None:
        app = XStarter(
            mode="live",
            environ={"X_STARTER_ENABLE_LIVE": "1", "X_BEARER_TOKEN": "not-a-real-token"},
        )
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        result = app.call_tool(
            "schedule_post",
            {"content": "Review only", "scheduled_for": future},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "LIVE_SCHEDULING_NOT_ENABLED")

    def test_invalid_mode_is_rejected(self) -> None:
        with self.assertRaises(StarterError) as caught:
            XStarter(mode="production", environ={})
        self.assertEqual(caught.exception.code, "INVALID_MODE")

    def test_browser_mode_requires_gate(self) -> None:
        with self.assertRaises(StarterError) as caught:
            XStarter(mode="browser", environ={})
        self.assertEqual(caught.exception.code, "BROWSER_NOT_ENABLED")

    def test_conformance_cannot_enable_live_mode(self) -> None:
        with self.assertRaises(StarterError) as caught:
            XStarter(
                mode="live",
                environ={
                    "X_STARTER_CONFORMANCE": "1",
                    "X_STARTER_ENABLE_LIVE": "1",
                    "X_BEARER_TOKEN": "not-a-real-token",
                },
            )
        self.assertEqual(caught.exception.code, "CONFORMANCE_REQUIRES_MOCK")


class McpProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = XStarter(mode="mock", environ={})

    def test_initialize_and_tools_call(self) -> None:
        init = handle_message(
            self.app,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            },
        )
        self.assertEqual(init["result"]["serverInfo"]["name"], "x-starter")
        self.assertIn(
            init["result"]["protocolVersion"],
            {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07"},
        )
        call = handle_message(
            self.app,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "account_summary", "arguments": {}},
            },
        )
        self.assertFalse(call["result"]["isError"])
        self.assertTrue(call["result"]["structuredContent"]["ok"])

    def test_discovery_has_all_primitive_kinds(self) -> None:
        capabilities = handle_message(
            self.app,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            },
        )["result"]["capabilities"]
        self.assertIn("tools", capabilities)
        self.assertIn("resources", capabilities)
        self.assertIn("prompts", capabilities)
        self.assertIn("completions", capabilities)
        resources = handle_message(
            self.app,
            {"jsonrpc": "2.0", "id": 2, "method": "resources/list", "params": {}},
        )
        self.assertGreaterEqual(len(resources["result"]["resources"]), 2)
        prompts = handle_message(
            self.app,
            {"jsonrpc": "2.0", "id": 3, "method": "prompts/list", "params": {}},
        )
        self.assertGreaterEqual(len(prompts["result"]["prompts"]), 2)

    def test_unknown_method_is_jsonrpc_error(self) -> None:
        response = handle_message(
            self.app,
            {"jsonrpc": "2.0", "id": 9, "method": "missing/method", "params": {}},
        )
        self.assertEqual(response["error"]["code"], -32601)

    def test_prompt_requires_declared_arguments(self) -> None:
        response = handle_message(
            self.app,
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "prompts/get",
                "params": {"name": "review_post", "arguments": {}},
            },
        )
        self.assertEqual(response["error"]["code"], -32602)

    def test_unknown_resource_uses_resource_not_found_code(self) -> None:
        response = handle_message(
            self.app,
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "resources/read",
                "params": {"uri": "xstarter://missing"},
            },
        )
        self.assertEqual(response["error"]["code"], -32002)


class HttpTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        app = XStarter(mode="mock", environ={})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_http_handler(app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _post(
        self,
        message: dict,
        *,
        headers: dict[str, str] | None = None,
    ) -> Request:
        body = json.dumps(message).encode("utf-8")
        merged = {"Content-Type": "application/json"}
        merged.update(headers or {})
        return Request(
            self.base + "/mcp",
            data=body,
            headers=merged,
            method="POST",
        )

    def _initialize(self) -> str:
        request = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            }
        )
        with urlopen(request, timeout=3) as response:
            result = json.loads(response.read().decode("utf-8"))
            session_id = response.headers["Mcp-Session-Id"]
        self.assertEqual(result["result"]["protocolVersion"], "2025-11-25")
        initialized = self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            headers={"Mcp-Session-Id": session_id},
        )
        with urlopen(initialized, timeout=3) as response:
            self.assertEqual(response.status, 202)
        return session_id

    def test_health_and_session_tool_call(self) -> None:
        with urlopen(self.base + "/health", timeout=3) as response:
            health = json.loads(response.read().decode("utf-8"))
        self.assertTrue(health["ok"])
        self.assertEqual(health["mode"], "mock")
        session_id = self._initialize()
        request = self._post(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "account_summary", "arguments": {}},
            },
            headers={"Mcp-Session-Id": session_id},
        )
        with urlopen(request, timeout=3) as response:
            result = json.loads(response.read().decode("utf-8"))
        self.assertTrue(result["result"]["structuredContent"]["ok"])
        self.assertTrue(result["result"]["structuredContent"]["meta"]["simulated"])

    def test_notification_returns_202(self) -> None:
        session_id = self._initialize()
        request = self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            headers={"Mcp-Session-Id": session_id},
        )
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 202)

    def test_tool_call_before_initialized_notification_is_rejected(self) -> None:
        request = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            }
        )
        with urlopen(request, timeout=3) as response:
            session_id = response.headers["Mcp-Session-Id"]
        request = self._post(
            {"jsonrpc": "2.0", "id": 12, "method": "tools/list", "params": {}},
            headers={"Mcp-Session-Id": session_id},
        )
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["error"]["code"], -32002)

    def test_request_without_session_returns_404(self) -> None:
        request = self._post(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=3)
        self.assertEqual(caught.exception.code, 404)

    def test_delete_ends_session(self) -> None:
        session_id = self._initialize()
        request = Request(
            self.base + "/mcp",
            headers={"Mcp-Session-Id": session_id},
            method="DELETE",
        )
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 204)
        request = self._post(
            {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
            headers={"Mcp-Session-Id": session_id},
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=3)
        self.assertEqual(caught.exception.code, 404)

    def test_foreign_origin_is_rejected(self) -> None:
        request = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            },
            headers={"Origin": "https://attacker.example"},
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=3)
        self.assertEqual(caught.exception.code, 403)

    def test_unsupported_version_header_is_rejected(self) -> None:
        session_id = self._initialize()
        request = self._post(
            {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
            headers={
                "Mcp-Session-Id": session_id,
                "MCP-Protocol-Version": "1900-01-01",
            },
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=3)
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(caught.exception.headers.get("Mcp-Error-Code"), "-32020")

    def test_different_supported_version_header_is_accepted(self) -> None:
        session_id = self._initialize()
        request = self._post(
            {"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": {}},
            headers={
                "Mcp-Session-Id": session_id,
                "MCP-Protocol-Version": "2025-03-26",
            },
        )
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read())
        self.assertIn("result", payload)
        self.assertIn("tools", payload["result"])

    def _open_stream(self, session_id: str) -> socket.socket:
        host, port = self.server.server_address
        stream = socket.create_connection((host, port), timeout=3)
        request = (
            "GET /mcp HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Accept: text/event-stream\r\n"
            f"Mcp-Session-Id: {session_id}\r\n"
            "MCP-Protocol-Version: 2025-11-25\r\n"
            "\r\n"
        )
        stream.sendall(request.encode("ascii"))
        deadline = time.monotonic() + 3
        received = b""
        while b"\r\n\r\n" not in received and time.monotonic() < deadline:
            received += stream.recv(4096)
        self.assertIn(b"200 OK", received)
        self.assertIn(b"text/event-stream", received)
        return stream

    def _read_event(self, stream: socket.socket, timeout: float = 3) -> dict:
        stream.settimeout(timeout)
        buffer = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            buffer += stream.recv(4096)
            for event in buffer.split(b"\n\n"):
                for line in event.splitlines():
                    if line.startswith(b"data: "):
                        return json.loads(line[6:])
        self.fail("No SSE data event received")

    def test_resource_subscription_delivers_live_notification(self) -> None:
        session_id = self._initialize()
        subscribe = self._post(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "resources/subscribe",
                "params": {"uri": "xstarter://session/current"},
            },
            headers={"Mcp-Session-Id": session_id},
        )
        with urlopen(subscribe, timeout=3) as response:
            self.assertTrue(json.loads(response.read())["result"] == {})
        stream = self._open_stream(session_id)
        try:
            call = self._post(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {"name": "x_account_get", "arguments": {}},
                },
                headers={"Mcp-Session-Id": session_id},
            )
            with urlopen(call, timeout=3) as response:
                self.assertTrue(json.loads(response.read())["result"]["structuredContent"]["ok"])
            event = self._read_event(stream)
            self.assertEqual(event["method"], "notifications/resources/updated")
            self.assertEqual(event["params"]["uri"], "xstarter://session/current")
        finally:
            stream.close()

    def test_unknown_resource_subscription_is_rejected(self) -> None:
        session_id = self._initialize()
        request = self._post(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "resources/subscribe",
                "params": {"uri": "xstarter://missing"},
            },
            headers={"Mcp-Session-Id": session_id},
        )
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["error"]["code"], -32602)

class ConformanceIsolationTest(unittest.TestCase):
    def test_fixture_profile_replaces_business_tools(self) -> None:
        app = XStarter(mode="mock", environ={"X_STARTER_CONFORMANCE": "1"})
        names = {tool["name"] for tool in app.tool_definitions()}
        self.assertIn("test_simple_text", names)
        self.assertNotIn("x_post_publish", names)


class BrowserBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.app = XStarter(
            mode="browser",
            environ={
                "X_STARTER_ENABLE_BROWSER": "1",
                "X_STARTER_STATE_DIR": self.temp.name,
            },
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _complete(self, action: dict, result: dict) -> dict:
        response = self.app.call_tool(
            "x_browser_complete_action",
            {
                "action_id": action["action_id"],
                "claim_token": action["claim_token"],
                "outcome": "succeeded",
                "result": result,
            },
        )
        self.assertTrue(response["ok"], response)
        return response["data"]

    def _observe_account(self) -> None:
        queued = self.app.call_tool("x_account_get", {"refresh": True})
        self.assertTrue(queued["ok"], queued)
        claimed = self.app.call_tool("x_browser_next_action", {})
        self.assertTrue(claimed["ok"], claimed)
        self._complete(
            claimed["data"]["action"],
            {
                "username": "guoz7777",
                "user_id": "1712345678901234567",
                "display_name": "guoz777",
                "posts_count": 5,
                "recent_post_ids": ["1900000000000000001"],
            },
        )
        account = self.app.call_tool("x_account_get", {})
        self.assertTrue(account["ok"], account)
        self.assertEqual(account["data"]["username"], "guoz7777")

    def test_observation_is_content_addressed_and_evidence_verifies(self) -> None:
        self._observe_account()
        evidence = self.app.call_tool("x_evidence_get", {"limit": 200})
        self.assertTrue(evidence["ok"], evidence)
        self.assertTrue(evidence["data"]["valid"])
        snapshot = self.app.call_tool("x_account_get", {})["data"]
        candidate = {
            key: snapshot[key]
            for key in (
                "username",
                "user_id",
                "display_name",
                "posts_count",
                "recent_post_ids",
                "observed_at",
            )
        }
        canonical = json.dumps(
            candidate,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(
            snapshot["snapshot_sha256"], hashlib.sha256(canonical).hexdigest()
        )

    def test_publish_requires_confirmation_and_verified_result(self) -> None:
        self._observe_account()
        prepared = self.app.call_tool(
            "x_post_prepare",
            {
                "text": "MCPBRIDGE-test publish",
                "expected_username": "guoz7777",
                "marker": "MCPBRIDGE-test",
            },
        )
        self.assertTrue(prepared["ok"], prepared)
        record = prepared["data"]
        wrong = self.app.call_tool(
            "x_post_publish",
            {"write_id": record["write_id"], "confirmation": "PUBLISH WRONG"},
        )
        self.assertFalse(wrong["ok"])
        self.assertEqual(wrong["error"]["code"], "CONFIRMATION_MISMATCH")
        published = self.app.call_tool(
            "x_post_publish",
            {
                "write_id": record["write_id"],
                "confirmation": record["confirmation_phrase"],
            },
        )
        self.assertTrue(published["ok"], published)
        claimed = self.app.call_tool("x_browser_next_action", {})
        action = claimed["data"]["action"]
        completed = self._complete(
            action,
            {
                "post_url": "https://x.com/guoz7777/status/2110000000000000001",
                "author": "guoz7777",
                "text": record["text"],
                "text_sha256": record["text_sha256"],
            },
        )
        self.assertTrue(completed["result"]["verified"])

    def test_marker_must_be_a_prefix(self) -> None:
        self._observe_account()
        result = self.app.call_tool(
            "x_post_prepare",
            {
                "text": "prefix is elsewhere: MCPBRIDGE-test",
                "expected_username": "guoz7777",
                "marker": "MCPBRIDGE-test",
            },
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")

    def test_publish_prepare_is_idempotent_with_key(self) -> None:
        self._observe_account()
        payload = {
            "text": "idempotent prepare",
            "expected_username": "guoz7777",
            "idempotency_key": "prepare-once-0001",
        }
        first = self.app.call_tool("x_post_prepare", payload)
        second = self.app.call_tool("x_post_prepare", payload)
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertEqual(first["data"]["write_id"], second["data"]["write_id"])
        self.assertEqual(
            first["data"]["confirmation_phrase"],
            second["data"]["confirmation_phrase"],
        )

    def test_write_record_is_scoped_to_its_session(self) -> None:
        self._observe_account()
        prepared = self.app.call_tool(
            "x_post_prepare",
            {"text": "session scoped"},
            session_id="session-a",
        )
        self.assertTrue(prepared["ok"], prepared)
        write = prepared["data"]
        hidden = self.app.call_tool(
            "x_post_publish",
            {
                "write_id": write["write_id"],
                "confirmation": write["confirmation_phrase"],
            },
            session_id="session-b",
        )
        self.assertFalse(hidden["ok"])
        self.assertEqual(hidden["error"]["code"], "WRITE_NOT_FOUND")

    def test_wrong_published_text_fails_closed(self) -> None:
        self._observe_account()
        prepared = self.app.call_tool(
            "x_post_prepare",
            {"text": "expected text", "expected_username": "guoz7777"},
        )["data"]
        queued = self.app.call_tool(
            "x_post_publish",
            {
                "write_id": prepared["write_id"],
                "confirmation": prepared["confirmation_phrase"],
            },
        )
        self.assertTrue(queued["ok"], queued)
        action = self.app.call_tool("x_browser_next_action", {})["data"]["action"]
        result = self.app.call_tool(
            "x_browser_complete_action",
            {
                "action_id": action["action_id"],
                "claim_token": action["claim_token"],
                "outcome": "succeeded",
                "result": {
                    "post_url": "https://x.com/guoz7777/status/2110000000000000002",
                    "author": "guoz7777",
                    "text": "wrong text",
                    "text_sha256": hashlib.sha256(b"wrong text").hexdigest(),
                },
            },
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "TEXT_MISMATCH")

    def test_uncertain_action_does_not_produce_success(self) -> None:
        self._observe_account()
        prepared = self.app.call_tool(
            "x_post_prepare",
            {"text": "uncertain publish", "expected_username": "guoz7777"},
        )["data"]
        self.app.call_tool(
            "x_post_publish",
            {
                "write_id": prepared["write_id"],
                "confirmation": prepared["confirmation_phrase"],
            },
        )
        action = self.app.call_tool("x_browser_next_action", {})["data"]["action"]
        result = self.app.call_tool(
            "x_browser_complete_action",
            {
                "action_id": action["action_id"],
                "claim_token": action["claim_token"],
                "outcome": "uncertain",
                "error": {
                    "code": "RESULT_UNKNOWN",
                    "message": "Composer closed before the post could be verified.",
                },
            },
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["status"], "uncertain")

    def test_delete_requires_absence_verification(self) -> None:
        self._observe_account()
        prepared = self.app.call_tool(
            "x_post_delete",
            {
                "post_url": "https://x.com/guoz7777/status/2110000000000000003",
                "expected_username": "guoz7777",
                "expected_text": "temporary post",
            },
        )
        self.assertTrue(prepared["ok"], prepared)
        record = prepared["data"]
        queued = self.app.call_tool(
            "x_post_delete",
            {
                "confirm": True,
                "write_id": record["write_id"],
                "confirmation": record["confirmation_phrase"],
            },
        )
        self.assertTrue(queued["ok"], queued)
        action = self.app.call_tool("x_browser_next_action", {})["data"]["action"]
        completed = self._complete(
            action,
            {
                "post_url": record["post_url"],
                "verified_absent": True,
                "verification": "reload_returned_post_not_found",
                "observed_text_sha256": hashlib.sha256(b"temporary post").hexdigest(),
            },
        )
        self.assertTrue(completed["result"]["verified_absent"])

    def test_delete_rejects_mismatched_text_before_deletion(self) -> None:
        self._observe_account()
        record = self.app.call_tool(
            "x_post_delete",
            {
                "post_url": "https://x.com/guoz7777/status/2110000000000000004",
                "expected_username": "guoz7777",
                "expected_text": "temporary post",
            },
        )["data"]
        self.app.call_tool(
            "x_post_delete",
            {
                "confirm": True,
                "write_id": record["write_id"],
                "confirmation": record["confirmation_phrase"],
            },
        )
        action = self.app.call_tool("x_browser_next_action", {})["data"]["action"]
        result = self.app.call_tool(
            "x_browser_complete_action",
            {
                "action_id": action["action_id"],
                "claim_token": action["claim_token"],
                "outcome": "succeeded",
                "result": {
                    "post_url": record["post_url"],
                    "verified_absent": True,
                    "observed_text_sha256": hashlib.sha256(b"wrong text").hexdigest(),
                },
            },
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "TEXT_MISMATCH")

    def test_observation_required_before_write(self) -> None:
        result = self.app.call_tool("x_post_prepare", {"text": "blocked"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "ACCOUNT_OBSERVATION_REQUIRED")

    def test_timeline_hash_depends_only_on_post_content(self) -> None:
        post = {
            "url": "https://x.com/guoz7777/status/2112242496441061458",
            "author": "guoz7777",
            "text": "same post",
            "created_at": "2026-10-19T18:00:29.000Z",
        }
        first = self.app.bridge.record_timeline([post])
        second = self.app.bridge.record_timeline([post])
        self.assertEqual(first["snapshot_sha256"], second["snapshot_sha256"])

    def test_next_action_marks_expired_lease_uncertain(self) -> None:
        self._observe_account()
        queued = self.app.call_tool("x_post_prepare", {"text": "lease test"})
        self.assertTrue(queued["ok"], queued)
        self.app.call_tool(
            "x_post_publish",
            {
                "write_id": queued["data"]["write_id"],
                "confirmation": queued["data"]["confirmation_phrase"],
            },
        )
        action = self.app.call_tool("x_browser_next_action", {})["data"]["action"]
        state_path = Path(self.temp.name) / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["actions"][action["action_id"]]["lease_expires_epoch"] = 0
        state_path.write_text(json.dumps(state), encoding="utf-8")
        result = self.app.call_tool("x_browser_next_action", {})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["action"]["status"], "uncertain")


if __name__ == "__main__":
    unittest.main(verbosity=2)
