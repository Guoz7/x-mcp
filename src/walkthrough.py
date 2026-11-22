#!/usr/bin/env python3
"""Four-step, credential-free walkthrough over JSON-RPC messages."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from server import XStarter, handle_message


def _call(app: XStarter, request_id: int, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    response = handle_message(
        app,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )
    if not response or "result" not in response:
        raise RuntimeError(f"Invalid MCP response for {name}: {response}")
    structured = response["result"].get("structuredContent", {})
    if not structured.get("ok"):
        raise RuntimeError(f"{name} failed: {structured}")
    return structured


def run_walkthrough(app: XStarter | None = None) -> int:
    app = app or XStarter(mode="mock", environ={})
    initialized = handle_message(
        app,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "clientInfo": {"name": "x-starter-walkthrough", "version": "0.1.0"},
                "capabilities": {},
            },
        },
    )
    tools = handle_message(
        app,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    if not initialized or not tools:
        raise RuntimeError("MCP initialization failed")

    account = _call(app, 10, "account_summary")
    draft = _call(
        app,
        11,
        "draft_post",
        {
            "topic": "a 20-minute workflow for turning one build note into a clear X thread",
            "tone": "technical",
            "cta": "Reply with the step you would test first.",
        },
    )
    scheduled_for = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=1, minute=0, second=0, microsecond=0
    )
    schedule = _call(
        app,
        12,
        "schedule_post",
        {"content": draft["data"]["content"], "scheduled_for": scheduled_for.isoformat()},
    )
    metrics = _call(app, 13, "recent_metrics", {"days": 7})
    handoff = _call(
        app,
        14,
        "ahan_handoff",
        {
            "creator_alias": "mock_creator",
            "goal": "Find one collaboration task after the first post is reviewed.",
            "capabilities": ["x_drafting", "x_scheduling_review", "metrics_reading"],
        },
    )

    step_results = [
        {
            "step": 1,
            "name": "Connect and inspect",
            "tool": "account_summary",
            "result": account["data"],
        },
        {
            "step": 2,
            "name": "Draft one post",
            "tool": "draft_post",
            "result": draft["data"],
        },
        {
            "step": 3,
            "name": "Queue for review",
            "tool": "schedule_post",
            "result": schedule["data"],
        },
        {
            "step": 4,
            "name": "Measure and prepare AHAN handoff",
            "tools": ["recent_metrics", "ahan_handoff"],
            "result": {"metrics": metrics["data"], "ahan": handoff["data"]},
        },
    ]
    layers: list[str] = []
    for item in (account, draft, schedule, metrics, handoff):
        for event in item["meta"]["trace"]["events"]:
            if event["layer"] not in layers:
                layers.append(event["layer"])
    output = {
        "ok": True,
        "mode": app.mode,
        "server": initialized["result"]["serverInfo"],
        "tool_count": len(tools["result"]["tools"]),
        "steps": step_results,
        "trace_layers": layers,
        "disclosure": (
            "The entire walkthrough is simulated. No X or AHAN request was made."
        ),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_walkthrough())
