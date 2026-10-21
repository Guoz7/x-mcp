"""Deterministic local benchmark for the X Starter MCP core.

This benchmark does not contact X. It measures the protocol/application core
that runs before external network latency: discovery and representative reads.
Use it to compare changes, not as a claim about live X throughput.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from typing import Any, Callable

from server import XStarter, handle_message


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
    return ordered[index]


def _measure(name: str, iterations: int, operation: Callable[[], Any]) -> dict[str, Any]:
    samples: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        result = operation()
        elapsed_ms = (time.perf_counter() - start) * 1000
        if isinstance(result, dict) and result.get("ok") is False:
            raise RuntimeError(f"{name} returned an error: {result}")
        samples.append(elapsed_ms)
    total_ms = sum(samples)
    return {
        "iterations": iterations,
        "total_ms": round(total_ms, 3),
        "ops_per_second": round(iterations / (total_ms / 1000), 2) if total_ms else 0.0,
        "latency_ms": {
            "min": round(min(samples), 3),
            "p50": round(_percentile(samples, 0.50), 3),
            "p95": round(_percentile(samples, 0.95), 3),
            "p99": round(_percentile(samples, 0.99), 3),
            "max": round(max(samples), 3),
            "mean": round(statistics.fmean(samples), 3),
        },
    }


def run(iterations: int) -> dict[str, Any]:
    app = XStarter(mode="mock", environ={})
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-11-25"},
    }
    handle_message(app, initialize)
    results = {
        "tools_list": _measure(
            "tools/list",
            iterations,
            lambda: handle_message(
                app,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            ),
        ),
        "account_summary": _measure(
            "account_summary",
            iterations,
            lambda: app.call_tool("account_summary"),
        ),
        "timeline_observe": _measure(
            "x_timeline_observe",
            iterations,
            lambda: app.call_tool("x_timeline_observe", {"limit": 10}),
        ),
    }
    return {
        "benchmark": "x-starter-core",
        "mode": "mock",
        "network_calls": 0,
        "iterations": iterations,
        "results": results,
        "interpretation": "Core-only baseline. It excludes browser, OAuth, disk, and X API latency.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark the local X Starter core")
    parser.add_argument("--iterations", type=int, default=500)
    args = parser.parse_args()
    if args.iterations < 10 or args.iterations > 100_000:
        parser.error("--iterations must be between 10 and 100000")
    print(json.dumps(run(args.iterations), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
