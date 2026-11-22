#!/usr/bin/env python3
"""Minimal Streamable HTTP MCP client for local verification."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def _post(
    url: str,
    message: dict[str, Any],
    *,
    session_id: str | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    request = Request(
        url,
        data=json.dumps(message, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
            if response.status == 202 or not body:
                return dict(response.headers), {}
            return dict(response.headers), json.loads(body)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:1000]}") from None


def _unwrap(response: dict[str, Any]) -> Any:
    if "error" in response:
        raise RuntimeError(json.dumps(response["error"], ensure_ascii=False))
    result = response.get("result")
    if isinstance(result, dict) and "structuredContent" in result:
        return result["structuredContent"]
    return result


def _load_json(path: str | None, inline: str) -> Any:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    return json.loads(inline)


def _wait_for_resource_event(
    url: str, session_id: str, uri: str, *, timeout: float
) -> dict[str, Any]:
    headers = {
        "Accept": "text/event-stream",
        "Mcp-Session-Id": session_id,
        "MCP-Protocol-Version": "2025-11-25",
    }
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=timeout) as response:
        deadline = time.monotonic() + timeout
        data_lines: list[str] = []
        while time.monotonic() < deadline:
            raw = response.readline()
            if not raw:
                break
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("data: "):
                data_lines.append(line[6:])
            elif not line and data_lines:
                event = json.loads("\n".join(data_lines))
                data_lines = []
                if (
                    event.get("method") == "notifications/resources/updated"
                    and event.get("params", {}).get("uri") == uri
                ):
                    return event
    raise RuntimeError(f"No resource update received for {uri}")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Call one local X Starter MCP tool")
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--method", default=None)
    parser.add_argument("--tool", default=None)
    parser.add_argument("--args", default="{}")
    parser.add_argument("--args-file", default=None)
    parser.add_argument("--params", default="{}")
    parser.add_argument("--params-file", default=None)
    parser.add_argument(
        "--subscribe-and-call",
        action="store_true",
        help="Subscribe to the current-session resource, open SSE, then call one tool.",
    )
    args = parser.parse_args(argv)

    headers, initialized = _post(
        args.url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "clientInfo": {"name": "x-starter-cli", "version": "0.1.0"},
                "capabilities": {},
            },
        },
    )
    session_id = headers.get("Mcp-Session-Id")
    if not session_id:
        raise RuntimeError("Server did not return Mcp-Session-Id")
    _post(
        args.url,
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        session_id=session_id,
    )

    if args.subscribe_and_call:
        if not args.tool:
            raise RuntimeError("--subscribe-and-call requires --tool")
        uri = "xstarter://session/current"
        _, subscribed = _post(
            args.url,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/subscribe",
                "params": {"uri": uri},
            },
            session_id=session_id,
        )
        if "error" in subscribed:
            raise RuntimeError(json.dumps(subscribed["error"], ensure_ascii=False))
        event_holder: dict[str, Any] = {}

        def listen() -> None:
            event_holder["event"] = _wait_for_resource_event(
                args.url, session_id, uri, timeout=10
            )

        listener = threading.Thread(target=listen, daemon=True)
        listener.start()
        time.sleep(0.2)
        _, response = _post(
            args.url,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": args.tool,
                    "arguments": _load_json(args.args_file, args.args),
                },
            },
            session_id=session_id,
        )
        listener.join(timeout=11)
        if "event" not in event_holder:
            raise RuntimeError("Resource update was not observed")
        sys.stdout.write(
            json.dumps(
                {
                    "server": initialized["result"]["serverInfo"],
                    "subscription": {"uri": uri, "event": event_holder["event"]},
                    "result": _unwrap(response),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        return 0

    if args.method:
        params = _load_json(args.params_file, args.params)
        message = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": args.method,
            "params": params,
        }
    elif args.tool:
        message = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": args.tool,
                "arguments": _load_json(args.args_file, args.args),
            },
        }
    else:
        message = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

    _, response = _post(args.url, message, session_id=session_id)
    sys.stdout.write(
        json.dumps(
            {
                "server": initialized["result"]["serverInfo"],
                "result": _unwrap(response),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
