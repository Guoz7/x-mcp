"""Portable packaging and generated configuration checks."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import browser_paths
import build_test_package
import client_config
import selfcheck
import tester


class DistributionTests(unittest.TestCase):
    def test_python_generator_does_not_select_developer_interpreter(self):
        with patch.dict(os.environ, {"X_STARTER_PYTHON": ""}):
            with patch.object(client_config, "SCRIPT_DIR", Path("missing-package-dir")):
                self.assertEqual(client_config._python(), sys.executable)

    def test_chrome_override_is_checked_by_shared_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "chrome.exe"
            executable.touch()
            with patch.dict(os.environ, {"X_BROWSER_EXECUTOR_CHROME": str(executable)}):
                self.assertEqual(browser_paths.find_chrome(), str(executable))
                self.assertEqual(selfcheck._chrome(), str(executable))
            with patch.dict(os.environ, {"X_BROWSER_EXECUTOR_CHROME": str(executable) + ".missing"}):
                self.assertIsNone(browser_paths.find_chrome())

    def test_writable_probe_detects_file_instead_of_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "not-a-directory"
            file.touch()
            self.assertFalse(selfcheck._state_writable(file))
            self.assertTrue(selfcheck._state_writable(Path(directory) / "state"))

    def test_configs_include_owner_without_exporting_inherited_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(tester, "ROOT", Path(directory)), patch.dict(os.environ, {
                "LOCALAPPDATA": directory, "MAGPIE_API_KEY": "test-marker-never-export",
                "X_BEARER_TOKEN": "test-marker-never-export", "X_STARTER_READ_BASE_URL": "http://localhost:1",
            }):
                tester.config()
                content = (Path(directory) / "mcp-client.json").read_text(encoding="utf-8")
                self.assertNotIn("test-marker-never-export", content)
                data = json.loads(content)["mcpServers"]["xstarter"]
                self.assertIn("X_STARTER_OWNER_PROFILE", data["env"])
                self.assertEqual(data["env"]["X_BROWSER_EXECUTOR_HEADLESS"], "0")
                self.assertNotIn("X_STARTER_READ_BASE_URL", data["env"])
                import tomllib
                parsed = tomllib.loads((Path(directory) / "mcp-codex.toml").read_text(encoding="utf-8"))
                self.assertEqual(parsed["mcp_servers"]["xstarter"]["args"], data["args"])

    def test_allowlist_excludes_secrets_and_state_and_private_harness(self):
        for name in build_test_package.FILES:
            path = Path(name)
            self.assertTrue((ROOT / path).is_file(), name)
            self.assertFalse(set(path.parts) & build_test_package.FORBIDDEN, name)
            self.assertNotIn("control-test-result", name)
            self.assertNotIn("acceptance.py", name)
        self.assertIn("requirements-browser.txt", build_test_package.FILES)
        self.assertIn("tester.cmd", build_test_package.FILES)
        self.assertIn("public_skill.md", build_test_package.FILES)
        self.assertIn("interaction_batch.py", build_test_package.FILES)
        self.assertIn("follow_batch.py", build_test_package.FILES)
        self.assertIn("interaction_batch_e2e.py", build_test_package.FILES)

    def test_failed_venv_creation_never_installs_into_global_python(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(tester, "ROOT", Path(directory)), patch.object(tester.venv, "EnvBuilder"), patch.object(tester.subprocess, "call") as call:
                with patch.object(sys, "argv", ["tester.py", "setup"]):
                    self.assertEqual(tester.main(), 2)
                call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
