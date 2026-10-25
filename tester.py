"""Portable Windows test entry point. Never changes a client's global config."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv

ROOT = Path(__file__).resolve().parent


def runtime() -> Path:
    candidate = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return candidate if candidate.is_file() else Path(sys.executable)


def environment() -> dict[str, str]:
    env = os.environ.copy()
    local = Path(env.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "x-starter-test"
    env.update({
        "X_STARTER_MODE": "browser",
        "X_STARTER_ENABLE_BROWSER": "1",
        "X_STARTER_PYTHON": str(runtime()),
        "X_STARTER_STATE_DIR": str(local / "state"),
        "X_STARTER_BROWSER_PROFILE_DIR": str(local / "browser-profiles"),
        "X_STARTER_OWNER_PROFILE": str(local / "owner-x"),
        "X_BROWSER_EXECUTOR_PERSISTENT": "1",
        "X_BROWSER_EXECUTOR_ALLOWED_HOSTS": "x.com,www.x.com,twitter.com,www.twitter.com,google.com,www.google.com",
        "X_BROWSER_EXECUTOR_GATED_HOSTS": "x.com,twitter.com",
        "X_BROWSER_EXECUTOR_PROFILE_HOSTS": "x.com,www.x.com,twitter.com,www.twitter.com",
    })
    env.pop("X_STARTER_READ_BASE_URL", None)
    env.pop("X_STARTER_CONFORMANCE", None)
    return env


def run_file(name: str, *args: str, env: dict[str, str] | None = None) -> int:
    return subprocess.call(
        [str(runtime()), "-I", "-X", "utf8", str(ROOT / name), *args],
        cwd=ROOT, env=env or environment(),
    )


def config() -> int:
    env = environment()
    fields = {key: value for key, value in env.items() if key.startswith(("X_STARTER_", "X_BROWSER_EXECUTOR_"))}
    # Use only the documented settings; do not publish inherited credentials.
    keys = (
        "X_STARTER_MODE", "X_STARTER_ENABLE_BROWSER", "X_STARTER_STATE_DIR",
        "X_STARTER_BROWSER_PROFILE_DIR", "X_STARTER_OWNER_PROFILE",
        "X_BROWSER_EXECUTOR_PERSISTENT", "X_BROWSER_EXECUTOR_GATED_HOSTS",
        "X_BROWSER_EXECUTOR_ALLOWED_HOSTS", "X_BROWSER_EXECUTOR_PROFILE_HOSTS",
    )
    fields = {key: fields[key] for key in keys}
    if env.get("X_BROWSER_EXECUTOR_CHROME", "").strip():
        fields["X_BROWSER_EXECUTOR_CHROME"] = env["X_BROWSER_EXECUTOR_CHROME"]
    fields["X_BROWSER_EXECUTOR_HEADLESS"] = "0"
    entry = {
        "command": str(runtime()),
        "args": ["-I", "-X", "utf8", str(ROOT / "server.py"), "--transport", "stdio"],
        "env": fields,
    }
    json_path = ROOT / "mcp-client.json"
    json_path.write_text(json.dumps({"mcpServers": {"xstarter": entry}}, indent=2), encoding="utf-8")
    def quote(value: str) -> str:
        return json.dumps(value, ensure_ascii=True)
    toml = (
        "[mcp_servers.xstarter]\n"
        f"command = {quote(entry['command'])}\n"
        f"args = {json.dumps(entry['args'])}\n\n"
        "[mcp_servers.xstarter.env]\n"
        + "".join(f"{key} = {quote(value)}\n" for key, value in fields.items())
    )
    toml_path = ROOT / "mcp-codex.toml"
    toml_path.write_text(toml, encoding="utf-8")
    print(f"Generated: {json_path}\nGenerated: {toml_path}")
    print("Merge ONE entry into your MCP client. Existing settings were not changed.")
    return 0


def smoke() -> int:
    # No owner profile, account data, network or browser is used here.
    with tempfile.TemporaryDirectory(prefix="xstarter-package-") as temporary:
        env = environment()
        env.update({"X_STARTER_MODE": "mock", "X_STARTER_STATE_DIR": temporary})
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "clientInfo": {"name": "tester-smoke", "version": "1"}, "capabilities": {},
            }},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "x_session_status", "arguments": {}}},
        ]
        result = subprocess.run(
            [str(runtime()), "-I", "-X", "utf8", str(ROOT / "server.py"), "--transport", "stdio"],
            input="".join(json.dumps(item) + "\n" for item in messages),
            capture_output=True, text=True, encoding="utf-8", timeout=60, env=env, cwd=ROOT,
        )
        replies = {item["id"]: item for line in result.stdout.splitlines() if (item := json.loads(line)).get("id")}
        tools = replies.get(2, {}).get("result", {}).get("tools", [])
        structured = replies.get(3, {}).get("result", {}).get("structuredContent", {})
        checks = {
            "process_exit_zero": result.returncode == 0,
            "initialize": "serverInfo" in replies.get(1, {}).get("result", {}),
            "browser_tools_discovered": {"browser_open", "browser_screenshot", "browser_close", "x_owner_read"} <= {item["name"] for item in tools},
            "mock_tool_call": structured.get("ok") is True and structured.get("mode") == "mock",
        }
        print(json.dumps({"scope": "real stdio MCP process; simulated X, no browser or network", "checks": checks, "tools": len(tools), "passed": all(checks.values())}, indent=2))
        return 0 if all(checks.values()) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "config", "doctor", "login", "check-login", "smoke", "http"))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("Python 3.11 or newer is required")
    if args.action == "setup":
        vpython = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not vpython.is_file():
            venv.EnvBuilder(with_pip=True).create(ROOT / ".venv")
        if not vpython.is_file():
            print("Local environment creation failed; global Python was not changed.", file=sys.stderr)
            return 2
        code = subprocess.call([str(vpython), "-m", "pip", "install", "-r", str(ROOT / "requirements-browser.txt")])
        if code:
            return code
        return config()
    if args.action == "config":
        return config()
    if args.action == "smoke":
        return smoke()
    if args.action == "doctor":
        return run_file("selfcheck.py", "--require-browser")
    if args.action in {"login", "check-login"}:
        env = environment()
        if args.action == "login":
            Path(env["X_STARTER_OWNER_PROFILE"]).mkdir(parents=True, exist_ok=True)
        env["X_BROWSER_EXECUTOR_HEADLESS"] = "0" if args.action == "login" else "1"
        return run_file("owner_login_open.py" if args.action == "login" else "owner_login_check.py", env=env)
    return run_file("server.py", "--transport", "http", "--host", "127.0.0.1", "--port", str(args.port))


if __name__ == "__main__":
    raise SystemExit(main())
