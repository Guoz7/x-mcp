#!/usr/bin/env python3
"""Concurrent Streamable HTTP load test for the X Starter MCP server.

This driver exercises the real HTTP transport instead of calling the protocol
core in process: every client performs the MCP session handshake, keeps one
HTTP/1.1 connection, alternates ``tools/list`` and ``tools/call``, and closes
its session with ``DELETE``. It reports throughput, per-method latency,
classified errors and the context cost of the published tool catalog.

It never contacts X. When ``--spawn`` is used it starts a mock server inside
this process and shuts it down before returning.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import io
import json
import math
import os
import statistics
import sys
import threading
import time
from collections import Counter
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from server import SessionRegistry, XStarter, make_http_handler

PROTOCOL_VERSION = "2025-11-25"
ACCEPT = "application/json, text/event-stream"
CLIENT_INFO = {"name": "x-starter-loadtest", "version": "0.1.0"}
# Small head start per client thread so the listen backlog is never hit at once.
CONNECT_STAGGER_S = 0.005
LARGEST_TOOL_COUNT = 5
CONTEXT_METHOD = (
    "bytes of compact JSON tools array / 4; approximation, not a tokenizer"
)


class _Target(NamedTuple):
    """Where the load test sends requests."""

    host: str
    port: int
    path: str

    @classmethod
    def parse(cls, url: str) -> "_Target":
        parts = urlsplit(url)
        if parts.scheme != "http" or not parts.hostname:
            raise ValueError(f"Unsupported MCP url: {url!r} (expected http://host:port/mcp)")
        return cls(parts.hostname, parts.port or 80, parts.path or "/mcp")


class Response(NamedTuple):
    """One HTTP round trip."""

    status: int | None
    headers: dict[str, str]
    payload: Any
    network_error: str | None
    connect_ms: float | None  # time spent on TCP connects for this message


# ---------------------------------------------------------------- statistics


def _nearest_rank(ordered: list[float], fraction: float) -> float:
    """Nearest-rank percentile: the sample at index ceil(fraction * n) - 1."""
    rank = math.ceil(fraction * len(ordered))
    index = min(len(ordered) - 1, max(0, rank - 1))
    return ordered[index]


def percentiles(samples: list[float]) -> dict[str, Any]:
    """Summarize millisecond samples with the nearest-rank percentile rule."""
    values = sorted(float(sample) for sample in samples)
    if not values:
        return {
            "count": 0,
            "min": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "max": 0.0,
            "mean": 0.0,
        }
    return {
        "count": len(values),
        "min": round(values[0], 3),
        "p50": round(_nearest_rank(values, 0.50), 3),
        "p95": round(_nearest_rank(values, 0.95), 3),
        "p99": round(_nearest_rank(values, 0.99), 3),
        "max": round(values[-1], 3),
        "mean": round(statistics.fmean(values), 3),
    }


def _compact_json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _approx_tokens(size_bytes: int) -> int:
    return int(round(size_bytes / 4))


def context_cost(tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Approximate how much context window a ``tools/list`` catalog consumes."""
    entries: list[dict[str, Any]] = []
    for tool in tools:
        size = _compact_json_bytes(tool)
        entries.append(
            {
                "name": str(tool.get("name", "")),
                "bytes": size,
                "approx_tokens": _approx_tokens(size),
            }
        )
    entries.sort(key=lambda item: (-item["bytes"], item["name"]))
    total_bytes = _compact_json_bytes(tools)
    return {
        "tools": len(tools),
        "tools_list_bytes": total_bytes,
        "approx_tokens": _approx_tokens(total_bytes),
        "largest_tools": entries[:LARGEST_TOOL_COUNT],
        "method": CONTEXT_METHOD,
    }


# ------------------------------------------------------------ http plumbing


def _parse_sse(raw: bytes) -> Any:
    """Return the last JSON-RPC message carried by an SSE body."""
    data_lines: list[str] = []
    last: Any = None
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif not line.strip() and data_lines:
            last = json.loads("\n".join(data_lines))
            data_lines = []
    if data_lines:
        last = json.loads("\n".join(data_lines))
    return last


def _parse_body(raw: bytes, content_type: str) -> Any:
    if "text/event-stream" in content_type.lower():
        return _parse_sse(raw)
    return json.loads(raw.decode("utf-8"))


def _rpc_error_code(payload: Any) -> int | None:
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        code = payload["error"].get("code")
        if isinstance(code, int):
            return code
    return None


def _is_tool_error(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    result = payload.get("result")
    return isinstance(result, dict) and bool(result.get("isError"))


def _classify(response: Response, *, check_tool_error: bool = False) -> str | None:
    """Map one response to an error kind, or ``None`` when it is acceptable."""
    if response.network_error is not None or response.status is None:
        return "network"
    code = _rpc_error_code(response.payload)
    if code is not None:
        return f"jsonrpc_{code}"
    if response.status < 200 or response.status >= 300:
        return f"http_{response.status}"
    if check_tool_error and _is_tool_error(response.payload):
        return "tool_error"
    return None


class _Client:
    """A single MCP client: one keep-alive connection plus a session id."""

    def __init__(self, target: _Target, timeout: float) -> None:
        self._target = target
        self._timeout = timeout
        self._conn: http.client.HTTPConnection | None = None

    def _drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()

    def close(self) -> None:
        self._drop()

    def _round_trip(
        self,
        message: dict[str, Any] | None,
        session_id: str | None,
        method: str,
        first_error: str | None,
        connect_ms: float | None,
    ) -> Response:
        conn = self._conn
        if conn is None:
            raise http.client.NotConnected("connection is not open")
        headers = {
            "Content-Type": "application/json",
            "Accept": ACCEPT,
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        body = (
            None
            if message is None
            else json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        conn.request(method, self._target.path, body=body, headers=headers)
        raw_response = conn.getresponse()
        raw = raw_response.read()
        status = raw_response.status
        received = {key.lower(): value for key, value in raw_response.getheaders()}
        # Streamable HTTP answers with JSON or with a one-shot SSE body that
        # closes the connection. Reconnect before the next request instead of
        # treating that close as a transport fault.
        if raw_response.will_close or received.get("connection", "").lower() == "close":
            self._drop()
        payload: Any = None
        if raw:
            try:
                payload = _parse_body(raw, received.get("content-type", ""))
            except ValueError as exc:
                return Response(
                    status,
                    received,
                    None,
                    first_error or f"invalid body: {exc}",
                    connect_ms,
                )
        return Response(status, received, payload, first_error, connect_ms)

    def send(
        self,
        message: dict[str, Any] | None,
        *,
        session_id: str | None = None,
        method: str = "POST",
    ) -> Response:
        """Send one message; on a transport failure reconnect once and retry."""
        first_error: str | None = None
        connect_ms: float | None = None
        for attempt in range(2):
            try:
                if self._conn is None:
                    connect_started = time.perf_counter()
                    conn = http.client.HTTPConnection(
                        self._target.host, self._target.port, timeout=self._timeout
                    )
                    conn.connect()
                    self._conn = conn
                    spent = (time.perf_counter() - connect_started) * 1000
                    connect_ms = spent if connect_ms is None else connect_ms + spent
                return self._round_trip(message, session_id, method, first_error, connect_ms)
            except (http.client.HTTPException, OSError) as exc:
                self._drop()
                if first_error is None:
                    first_error = f"{type(exc).__name__}: {exc}"
                if attempt == 1:
                    return Response(None, {}, None, first_error, connect_ms)
        return Response(None, {}, None, first_error, connect_ms)


# ------------------------------------------------------------- load driver


def _spawn_server(clients: int) -> tuple[Any, threading.Thread, str]:
    app = XStarter(mode="mock", environ={})
    handler = make_http_handler(app, SessionRegistry(limit=clients + 8))
    server_class = getattr(handler, "mcp_server_class", ThreadingHTTPServer)
    server = server_class(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    return server, thread, f"http://{host}:{port}/mcp"


def _tool_mode(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("mode"), str):
        return structured["mode"]
    return None


def _tool_catalog(payload: Any) -> list[dict[str, Any]] | None:
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    tools = result.get("tools")
    if isinstance(tools, list) and all(isinstance(tool, dict) for tool in tools):
        return tools
    return None


def _client_run(
    index: int,
    target: _Target,
    tool: str,
    arguments: dict[str, Any],
    requests: int,
    timeout: float,
) -> dict[str, Any]:
    """Run one client session and return its local counters and samples."""
    if index:
        time.sleep(index * CONNECT_STAGGER_S)
    client = _Client(target, timeout)
    errors: Counter[str] = Counter()
    latencies: dict[str, list[float]] = {"tools_list": [], "tools_call": []}
    outcome: dict[str, Any] = {
        "handshake_ms": None,
        "latencies": latencies,
        "connect_ms": [],
        "errors": errors,
        "attempted": 0,
        "closed": 0,
        "tools": None,
        "mode": None,
    }
    connects: list[float] = outcome["connect_ms"]
    handshake_started = time.perf_counter()
    response = client.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "clientInfo": CLIENT_INFO,
                "capabilities": {},
            },
        }
    )
    if response.connect_ms is not None:
        connects.append(response.connect_ms)
    session_id = response.headers.get("mcp-session-id")
    kind = _classify(response)
    if kind is None and not session_id:
        kind = f"http_{response.status}"
    if kind is None:
        ready = client.send(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            session_id=session_id,
        )
        if ready.connect_ms is not None:
            connects.append(ready.connect_ms)
        kind = _classify(ready)
        if kind is None and ready.status != 202:
            kind = f"http_{ready.status}"
    if kind is not None:
        errors[kind] += 1
        client.close()
        return outcome
    outcome["handshake_ms"] = (time.perf_counter() - handshake_started) * 1000

    for step in range(requests):
        is_call = step % 2 == 1
        bucket = "tools_call" if is_call else "tools_list"
        if is_call:
            message = {
                "jsonrpc": "2.0",
                "id": step + 2,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        else:
            message = {
                "jsonrpc": "2.0",
                "id": step + 2,
                "method": "tools/list",
                "params": {},
            }
        started = time.perf_counter()
        answer = client.send(message, session_id=session_id)
        elapsed_ms = (time.perf_counter() - started) * 1000
        outcome["attempted"] += 1
        if answer.connect_ms is not None:
            connects.append(answer.connect_ms)
        if answer.status is not None:
            # Report the round trip on an established connection; the reconnect
            # cost is reported separately under connect_ms.
            elapsed_ms = max(0.0, elapsed_ms - (answer.connect_ms or 0.0))
            latencies[bucket].append(elapsed_ms)
        kind = _classify(answer, check_tool_error=is_call)
        if kind is not None:
            errors[kind] += 1
        elif is_call:
            mode = _tool_mode(answer.payload)
            if mode is not None:
                outcome["mode"] = mode
        else:
            catalog = _tool_catalog(answer.payload)
            if catalog is not None and outcome["tools"] is None:
                outcome["tools"] = catalog

    deleted = client.send(None, session_id=session_id, method="DELETE")
    if deleted.connect_ms is not None:
        connects.append(deleted.connect_ms)
    kind = _classify(deleted)
    if kind is None and deleted.status != 204:
        kind = f"http_{deleted.status}"
    if kind is not None:
        errors[kind] += 1
    else:
        outcome["closed"] = 1
    client.close()
    return outcome


def _load_phase(
    target: _Target,
    url: str,
    clients: int,
    requests: int,
    tool: str,
    arguments: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    results: list[dict[str, Any] | None] = [None] * clients

    def worker(index: int) -> None:
        try:
            results[index] = _client_run(index, target, tool, arguments, requests, timeout)
        except BaseException as exc:  # noqa: BLE001 - a crashed worker is still a result
            results[index] = {"crash": f"{type(exc).__name__}: {exc}"}

    workers = [
        threading.Thread(target=worker, args=(index,), daemon=True)
        for index in range(clients)
    ]
    started = time.perf_counter()
    for worker_thread in workers:
        worker_thread.start()
    for worker_thread in workers:
        worker_thread.join()
    duration_s = time.perf_counter() - started

    samples: dict[str, list[float]] = {"handshake": [], "tools_list": [], "tools_call": []}
    connects: list[float] = []
    errors: Counter[str] = Counter()
    attempts = 0
    sessions_closed = 0
    catalog: list[dict[str, Any]] | None = None
    mode: str | None = None
    crashes: list[str] = []
    for result in results:
        if result is None:
            continue
        if "crash" in result:
            crashes.append(str(result["crash"]))
            errors["network"] += 1
            continue
        if result["handshake_ms"] is not None:
            samples["handshake"].append(float(result["handshake_ms"]))
        connects.extend(result["connect_ms"])
        for bucket in ("tools_list", "tools_call"):
            samples[bucket].extend(result["latencies"][bucket])
        errors.update(result["errors"])
        attempts += int(result["attempted"])
        sessions_closed += int(result["closed"])
        if catalog is None and result["tools"] is not None:
            catalog = result["tools"]
        if mode is None and result["mode"] is not None:
            mode = result["mode"]

    total_errors = sum(errors.values())
    report: dict[str, Any] = {
        "benchmark": "x-starter-http-load",
        "transport": "streamable-http",
        "mode": mode or "unknown",
        "clients": clients,
        "requests_per_client": requests,
        "total_requests": attempts,
        "duration_s": round(duration_s, 3),
        "throughput_rps": round(attempts / duration_s, 2) if duration_s > 0 else 0.0,
        "errors": {
            "total": total_errors,
            "rate": round(total_errors / attempts, 6) if attempts else 0.0,
            "by_kind": dict(sorted(errors.items())),
        },
        "latency_ms": {
            "handshake": percentiles(samples["handshake"]),
            "tools_list": percentiles(samples["tools_list"]),
            "tools_call": percentiles(samples["tools_call"]),
        },
        "connect_ms": percentiles(connects),
        "connections": len(connects),
        "context_cost": context_cost(catalog or []),
        "sessions_closed": sessions_closed,
        "target_url": url,
        "tool": tool,
        "tool_arguments": arguments,
        "notes": [
            "handshake includes the first TCP connect; tools_list/tools_call are "
            "round trips on an established connection.",
            "connect_ms covers every TCP connect; with keep-alive this should "
            "equal one connect per client.",
        ],
    }
    if crashes:
        report["worker_errors"] = crashes
    return report


def run_load(
    url: str | None,
    clients: int = 16,
    requests: int = 50,
    tool: str = "account_summary",
    args: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Run the load test. ``url=None`` spawns an in-process mock server."""
    if clients < 1:
        raise ValueError("clients must be >= 1")
    if requests < 1:
        raise ValueError("requests must be >= 1")
    if timeout <= 0:
        raise ValueError("timeout must be > 0")
    arguments = dict(args or {})
    if url is None:
        server, thread, spawned_url = _spawn_server(clients)
        target = _Target.parse(spawned_url)
        try:
            # The mock server logs one line per request; keep stdout clean.
            with contextlib.redirect_stderr(io.StringIO()):
                return _load_phase(
                    target, spawned_url, clients, requests, tool, arguments, timeout
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    target = _Target.parse(url)
    return _load_phase(target, url, clients, requests, tool, arguments, timeout)


# ---------------------------------------------------------------- interface


def _write_json(path: str, payload: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Concurrent Streamable HTTP load test for the X Starter MCP server"
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Existing MCP endpoint, e.g. http://127.0.0.1:8765/mcp",
    )
    parser.add_argument(
        "--spawn",
        action="store_true",
        help="Start a local mock server in this process (default when --url is omitted)",
    )
    parser.add_argument("--clients", type=int, default=16)
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--tool", default="account_summary")
    parser.add_argument("--args", default="{}")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--out", default=None, help="Also write the JSON report to this path")
    args = parser.parse_args(argv)

    if args.url and args.spawn:
        parser.error("--url and --spawn are mutually exclusive")
    try:
        tool_arguments = json.loads(args.args)
    except json.JSONDecodeError as exc:
        parser.error(f"--args must be JSON: {exc}")
        raise SystemExit(2) from None
    if not isinstance(tool_arguments, dict):
        parser.error("--args must be a JSON object")
    if args.clients < 1 or args.requests < 1:
        parser.error("--clients and --requests must be >= 1")
    if args.timeout <= 0:
        parser.error("--timeout must be > 0")

    report = run_load(
        args.url if args.url else None,
        args.clients,
        args.requests,
        args.tool,
        tool_arguments,
        args.timeout,
    )
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.out:
        _write_json(args.out, report)
    return 2 if report["errors"]["total"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
