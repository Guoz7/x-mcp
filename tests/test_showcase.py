"""Offline tests for the one-command live showcase script."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (str(ROOT / "src"), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from scripts import showcase  # noqa: E402


class ShowcaseScriptTests(unittest.TestCase):
    def test_run_showcase_no_browser_passes_with_valid_evidence(self) -> None:
        report = showcase.run_showcase(iterations=100, no_browser=True)
        self.assertTrue(report["passed"])
        self.assertEqual(report["mcp_core_benchmark"]["tools_discovered"], 65)
        self.assertTrue(report["real_chromium_browser_e2e"]["skipped"])
        orch = report["multi_strategy_orchestration"]
        self.assertTrue(orch["evidence_chain_valid"])
        self.assertGreaterEqual(orch["total_verified_actions"], 70)
        self.assertEqual(orch["follower_growth_7d"]["start"], 118)
        self.assertGreater(orch["follower_growth_7d"]["current"], 118)
        self.assertEqual(orch["fastest_strategy"]["confidence"], "measured")
        self.assertGreaterEqual(len(orch["leaderboard"]), 4)

    def test_cli_json_no_browser_exits_zero(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "-I",
                "-X",
                "utf8",
                str(ROOT / "scripts" / "showcase.py"),
                "--no-browser",
                "--iterations",
                "100",
                "--json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        parsed = json.loads(proc.stdout)
        self.assertTrue(parsed["passed"])
        self.assertEqual(parsed["mcp_core_benchmark"]["tools_discovered"], 65)


if __name__ == "__main__":
    unittest.main()
