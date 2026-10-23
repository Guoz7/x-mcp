"""Tests for the plug-and-play path: config generation and self-check."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import client_config  # noqa: E402
import selfcheck  # noqa: E402
from server import parse_args  # noqa: E402


class ClientConfigTests(unittest.TestCase):
    def test_codex_toml_escapes_windows_backslashes(self) -> None:
        output = client_config.build("codex")["toml"]
        # A single backslash would break TOML parsing on Windows paths.
        self.assertIn("\\\\", output)
        self.assertNotIn('command = "E:\\app', output)

    def test_codex_toml_is_valid_and_parses_into_one_table(self) -> None:
        toml = client_config.build("codex")["toml"]
        try:
            import tomllib
        except ImportError:  # pragma: no cover - Python < 3.11
            self.skipTest("tomllib unavailable")
        parsed = tomllib.loads(toml)
        self.assertIn("mcp_servers", parsed)
        server = parsed["mcp_servers"]["xstarter"]
        self.assertEqual(server["env"]["X_STARTER_MODE"], "browser")
        self.assertEqual(server["env"]["X_STARTER_ENABLE_BROWSER"], "1")
        self.assertTrue(server["args"][0].endswith("server.py"))

    def test_json_config_is_valid_for_claude_and_cursor(self) -> None:
        for kind in ("claude", "cursor"):
            data = client_config.build(kind)
            encoded = json.dumps(data)
            self.assertIn("xstarter", encoded)
            entry = data["mcpServers"]["xstarter"]
            self.assertEqual(entry["env"]["X_STARTER_MODE"], "browser")

    def test_owner_profile_is_only_emitted_when_it_exists(self) -> None:
        with patch.object(client_config, "_owner_profile_exists", return_value=False):
            env = client_config.stdio_entry(with_owner=False)["env"]
            self.assertNotIn("X_STARTER_OWNER_PROFILE", env)
        with patch.object(client_config, "_owner_profile_exists", return_value=True):
            env = client_config.stdio_entry(with_owner=True)["env"]
            if os.environ.get("LOCALAPPDATA"):
                self.assertIn("X_STARTER_OWNER_PROFILE", env)

    def test_state_dir_is_not_the_project_directory(self) -> None:
        env = client_config.stdio_entry(with_owner=False)["env"]
        if "X_STARTER_STATE_DIR" in env:
            self.assertNotIn(str(ROOT), env["X_STARTER_STATE_DIR"])


class SelfCheckTests(unittest.TestCase):
    def test_report_separates_public_reads_from_owner_account(self) -> None:
        report = selfcheck.run()
        self.assertIn("ready_for_public_reads", report)
        self.assertIn("ready_for_owner_account", report)
        self.assertFalse(report["owner_login_verified"])
        for check in report["checks"]:
            self.assertIn("check", check)
            self.assertIn("ok", check)
            if not check["ok"]:
                self.assertTrue(check["fix"], check)

    def test_playwright_probe_uses_a_real_import(self) -> None:
        # Do not assume the running interpreter has Playwright; the quality
        # gate uses a plain Python. Prove the probe answers with a bool.
        self.assertIsInstance(
            selfcheck._python_has_playwright(sys.executable), bool
        )
        # An interpreter that cannot even start must report False, not raise.
        self.assertFalse(selfcheck._python_has_playwright("definitely-not-a-python"))

    def test_missing_chrome_is_reported_with_a_fix(self) -> None:
        with patch.object(selfcheck, "_chrome", return_value=None):
            report = selfcheck.run()
        entry = next(item for item in report["checks"] if item["check"] == "chrome_binary")
        self.assertFalse(entry["ok"])
        self.assertIn("X_BROWSER_EXECUTOR_CHROME", entry["fix"])


class CliTests(unittest.TestCase):
    def test_browser_mode_is_accepted_on_the_command_line(self) -> None:
        self.assertEqual(parse_args(["--mode", "browser"]).mode, "browser")

    def test_print_config_accepts_known_client_names(self) -> None:
        for name in ("all", "codex", "claude", "cursor"):
            self.assertEqual(parse_args(["--print-config", name]).print_config, name)
        self.assertEqual(parse_args(["--print-config"]).print_config, "all")


if __name__ == "__main__":
    unittest.main()
