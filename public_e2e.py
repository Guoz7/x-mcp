#!/usr/bin/env python3
"""End-to-end check of the credential-free public read tools over real HTTP.

This script starts nothing. Run the server first, then run this script. It uses
the same Streamable HTTP client path as any other MCP client, and it prints one
PASS/FAIL line per check plus a JSON summary.

Usage:

    python -X utf8 public_e2e.py --url http://127.0.0.1:8765/mcp --handle guoz7777
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mcp_client


def call(url: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    headers, _ = mcp_client._post(
        url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "clientInfo": {"name": "public-e2e", "version": "1.0.0"},
                "capabilities": {},
            },
        },
    )
    session_id = headers.get("Mcp-Session-Id")
    if not session_id:
        raise RuntimeError("server did not return Mcp-Session-Id")
    mcp_client._post(
        url,
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        session_id=session_id,
    )
    _, response = mcp_client._post(
        url,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        session_id=session_id,
    )
    result = mcp_client._unwrap(response)
    result["_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Public read E2E")
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--handle", default="guoz7777")
    parser.add_argument("--control-handle", default="NASA")
    parser.add_argument("--post", default="")
    args = parser.parse_args(argv)

    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: Any) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")

    profile = call(args.url, "x_public_profile", {"screen_name": args.handle})
    record(
        "profile",
        bool(profile.get("ok")),
        {
            "handle": profile.get("data", {}).get("profile", {}).get("screen_name"),
            "followers": profile.get("data", {}).get("profile", {}).get("metrics", {}).get("followers"),
            "credential": profile.get("data", {}).get("credential"),
            "simulated": profile.get("meta", {}).get("simulated"),
            "ms": profile.get("_elapsed_ms"),
        },
    )

    timeline = call(args.url, "x_public_timeline", {"screen_name": args.handle, "limit": 5})
    posts = timeline.get("data", {}).get("posts", [])
    record(
        "timeline",
        bool(timeline.get("ok")) and isinstance(posts, list),
        {"posts": len(posts), "first": posts[0]["url"] if posts else None, "ms": timeline.get("_elapsed_ms")},
    )

    control = call(args.url, "x_public_timeline", {"screen_name": args.control_handle, "limit": 5})
    control_posts = control.get("data", {}).get("posts", [])
    record(
        "control-account",
        bool(control.get("ok")) and len(control_posts) >= 1,
        {"handle": args.control_handle, "posts": len(control_posts)},
    )

    target = args.post or (posts[0]["url"] if posts else "")
    if target:
        single = call(args.url, "x_public_post", {"post": target})
        post = single.get("data", {}).get("post", {})
        record(
            "single-post",
            bool(single.get("ok")) and bool(post.get("text")),
            {"id": post.get("id"), "author": post.get("author"), "url": post.get("url")},
        )

    missing = call(args.url, "x_public_profile", {"screen_name": "zzznotreal9x8q"})
    record(
        "missing-account-is-an-error",
        not missing.get("ok") and missing.get("error", {}).get("code") == "PROFILE_NOT_FOUND",
        missing.get("error", {}).get("code"),
    )

    cached = call(args.url, "x_public_profile", {"screen_name": args.handle})
    record(
        "cache-reuse",
        bool(cached.get("ok"))
        and cached.get("data", {}).get("cache", {}).get("hit") in {True, False},
        cached.get("data", {}).get("cache"),
    )

    summary = {
        "passed": sum(1 for item in checks if item["ok"]),
        "failed": sum(1 for item in checks if not item["ok"]),
        "checks": checks,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
