"""Generate ready-to-paste MCP client configuration.

"Plug and play" fails when a user has to guess the transport, the URL, or the
environment block. This module prints the exact JSON each common host expects,
built from the paths that actually exist on this machine.

Secrets are never emitted. The owner profile path is emitted because the host
needs it to start the server; it contains no credential itself.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent


def _python() -> str:
    """The interpreter that has Playwright, which the executor needs."""

    configured = os.environ.get("X_STARTER_PYTHON", "").strip()
    if configured:
        return configured
    bundled = Path(r"E:\app\fingerprint-browser\venv\Scripts\python.exe")
    if bundled.exists():
        return str(bundled)
    return sys.executable


def _env_block(*, with_owner: bool) -> dict[str, str]:
    env = {
        "X_STARTER_MODE": "browser",
        "X_STARTER_ENABLE_BROWSER": "1",
        "X_BROWSER_EXECUTOR_HEADLESS": os.environ.get(
            "X_BROWSER_EXECUTOR_HEADLESS", "1"
        ),
    }
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        # Keep state and profiles off slow or removable volumes.
        env["X_STARTER_STATE_DIR"] = str(Path(local) / "x-starter" / "state" / "browser")
        env["X_STARTER_BROWSER_PROFILE_DIR"] = str(
            Path(local) / "x-starter" / "browser-profiles"
        )
        if with_owner:
            env["X_STARTER_OWNER_PROFILE"] = str(
                Path(local) / "x-starter" / "owner-profiles" / "owner-x"
            )
    return env


def _owner_profile_exists() -> bool:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if not local:
        return False
    return (Path(local) / "x-starter" / "owner-profiles" / "owner-x").is_dir()


def stdio_entry(*, with_owner: bool = True) -> dict[str, Any]:
    return {
        "command": _python(),
        "args": [str(SCRIPT_DIR / "server.py"), "--transport", "stdio"],
        "env": _env_block(with_owner=with_owner),
    }


def http_entry(*, port: int = 8765) -> dict[str, Any]:
    return {"url": f"http://127.0.0.1:{port}/mcp"}


def _toml_string(value: str) -> str:
    """TOML basic strings escape backslashes; Windows paths are full of them."""

    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build(kind: str, *, port: int = 8765) -> dict[str, Any]:
    owner = _owner_profile_exists()
    if kind == "codex":
        return {
            "note": "Merge into ~/.codex/config.toml as [mcp_servers.xstarter].",
            "toml": (
                "[mcp_servers.xstarter]\n"
                f"command = {_toml_string(_python())}\n"
                f"args = [{_toml_string(str(SCRIPT_DIR / 'server.py'))}, \"--transport\", \"stdio\"]\n"
                "\n[mcp_servers.xstarter.env]\n"
                + "".join(
                    f"{key} = {_toml_string(value)}\n"
                    for key, value in _env_block(with_owner=owner).items()
                )
            ),
        }
    if kind == "claude":
        return {"mcpServers": {"xstarter": stdio_entry(with_owner=owner)}}
    if kind == "cursor":
        return {"mcpServers": {"xstarter": stdio_entry(with_owner=owner)}}
    return {
        "mcpServers": {"xstarter": stdio_entry(with_owner=owner)},
        "http": {"xstarter": http_entry(port=port)},
        "notes": [
            "stdio is preferred: the client owns the process lifecycle.",
            "Use the http entry only when the client needs a URL.",
            "Set X_STARTER_OWNER_PROFILE only after signing in to that profile.",
        ],
    }


def as_text(kind: str, *, port: int = 8765) -> str:
    return json.dumps(build(kind, port=port), indent=2, ensure_ascii=False)
