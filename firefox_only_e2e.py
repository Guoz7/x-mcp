#!/usr/bin/env python3
"""Controlled browser-family E2E for the local MCP browser bridge.

This test deliberately does not read browser storage or launch a browser. It
tests the MCP boundary and executor gate over Streamable HTTP. Use
``--require-real-firefox`` only after a Firefox binary and a trusted UI
executor are installed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mcp_client  # noqa: E402


def _call(url: str, session_id: str, tool: str, arguments: dict[str, Any], request_id: int) -> Any:
    _, response = mcp_client._post(
        url,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        session_id=session_id,
    )
    return mcp_client._unwrap(response)


def _start_server(port: int, state_dir: str) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.update(
        {
            "X_STARTER_MODE": "browser",
            "X_STARTER_ENABLE_BROWSER": "1",
            "X_STARTER_BROWSER_FAMILY": "firefox",
            "X_STARTER_STATE_DIR": state_dir,
        }
    )
    return subprocess.Popen(
        [sys.executable, "-I", "-X", "utf8", "server.py", "--transport", "http", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def _wait_for_server(url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.time() + 15
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("browser-mode MCP server exited before the test started")
        try:
            request = __import__("urllib.request", fromlist=["Request"]).Request(url.replace("/mcp", "/health"))
            with __import__("urllib.request", fromlist=["urlopen"]).urlopen(request, timeout=1):
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("browser-mode MCP server did not become ready")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Firefox-only MCP browser bridge E2E")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--require-real-firefox", action="store_true")
    args = parser.parse_args(argv)

    firefox_binary = shutil.which("firefox") or shutil.which("firefox.exe")
    if args.require_real_firefox and not firefox_binary:
        print("BLOCKED real-firefox-binary :: Firefox is not installed or not on PATH")
        return 2

    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} {name} :: {detail}")

    with tempfile.TemporaryDirectory(prefix="x-starter-firefox-e2e-") as state_dir:
        process = _start_server(args.port, state_dir)
        url = f"http://127.0.0.1:{args.port}/mcp"
        try:
            _wait_for_server(url, process)
            headers, _ = mcp_client._post(
                url,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "clientInfo": {"name": "firefox-only-e2e", "version": "1.0.0"},
                        "capabilities": {"computer_use": True},
                    },
                },
            )
            session_id = headers["Mcp-Session-Id"]
            mcp_client._post(
                url,
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                session_id=session_id,
            )
            tools = _call(url, session_id, "connector_list", {}, 2)
            check("mcp-session", bool(tools.get("ok", True)), "initialized")

            queued = _call(url, session_id, "x_account_get", {"refresh": True}, 3)
            action_result = _call(url, session_id, "x_browser_next_action", {}, 4)
            action = action_result["data"]["action"]
            check(
                "firefox-policy-advertised",
                action["context_contract"]["allowed_browser_families"] == ["firefox"],
                action["context_contract"]["allowed_browser_families"],
            )

            completed = _call(
                url,
                session_id,
                "x_browser_complete_action",
                {
                    "action_id": action["action_id"],
                    "claim_token": action["claim_token"],
                    "outcome": "succeeded",
                    "result": {
                        "username": "guoz7777",
                        "user_id": "1597769294179627010",
                        "display_name": "guoz777",
                        "posts_count": 5,
                        "recent_post_ids": [],
                    },
                    "executor_context": {
                        "browser_family": "firefox",
                        "browser_version": "not-installed-contract-test",
                        "os": "windows",
                        "viewport": {"width": 1440, "height": 900},
                        "device_pixel_ratio": 1,
                        "zoom_percent": 100,
                        "locale": "zh-CN",
                        "color_scheme": "light",
                        "tab": {"id": "firefox-contract-tab", "url": "https://x.com/home"},
                        "account_hint": "guoz7777",
                    },
                    "locator_strategy": "accessibility",
                    "preconditions_checked": ["X host visible", "account hint visible"],
                    "postconditions_checked": ["account snapshot accepted"],
                },
                5,
            )
            check("firefox-completion", completed.get("ok", False), completed.get("data", {}).get("execution"))

            _call(url, session_id, "x_timeline_observe", {"refresh": True}, 6)
            second = _call(url, session_id, "x_browser_next_action", {}, 7)["data"]["action"]
            wrong = _call(
                url,
                session_id,
                "x_browser_complete_action",
                {
                    "action_id": second["action_id"],
                    "claim_token": second["claim_token"],
                    "outcome": "uncertain",
                    "error": {"code": "CONTROLLED_EXPERIMENT"},
                    "executor_context": {"browser_family": "chromium"},
                },
                8,
            )
            check(
                "non-firefox-rejected",
                wrong.get("ok") is False and wrong.get("error", {}).get("code") == "BROWSER_FAMILY_NOT_ALLOWED",
                wrong.get("error", {}).get("code"),
            )
            final = _call(
                url,
                session_id,
                "x_browser_complete_action",
                {
                    "action_id": second["action_id"],
                    "claim_token": second["claim_token"],
                    "outcome": "uncertain",
                    "error": {"code": "CONTROLLED_EXPERIMENT"},
                    "executor_context": {"browser_family": "firefox"},
                },
                9,
            )
            check("uncertain-recorded", final.get("ok", False) and final.get("data", {}).get("status") == "uncertain", final.get("data", {}).get("status"))

            summary = {
                "passed": sum(1 for item in checks if item["ok"]),
                "failed": sum(1 for item in checks if not item["ok"]),
                "firefox_binary": bool(firefox_binary),
                "real_browser_ui": False,
                "scope": "MCP contract and executor gate; no browser storage and no UI launched",
                "queued_account_observation": queued.get("data", {}).get("status"),
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0 if summary["failed"] == 0 else 1
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
