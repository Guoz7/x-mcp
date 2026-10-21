from __future__ import annotations

import json
import socket
import sys
import unittest
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loadtest import context_cost, percentiles, run_load  # noqa: E402
from server import XStarter  # noqa: E402


def _port_closed(url: str) -> bool:
    """True when nothing accepts a TCP connection on the URL's port."""
    parts = urlsplit(url)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        try:
            probe.connect((parts.hostname, parts.port))
        except OSError:
            return True
        return False


class PercentilesTest(unittest.TestCase):
    def test_empty_samples_are_all_zero(self) -> None:
        summary = percentiles([])
        self.assertEqual(
            summary,
            {
                "count": 0,
                "min": 0.0,
                "p50": 0.0,
                "p95": 0.0,
                "p99": 0.0,
                "max": 0.0,
                "mean": 0.0,
            },
        )

    def test_known_array_uses_nearest_rank(self) -> None:
        samples = [float(value) for value in range(1, 101)]
        summary = percentiles(samples)
        self.assertEqual(summary["count"], 100)
        self.assertEqual(summary["min"], 1.0)
        self.assertEqual(summary["p50"], 50.0)
        self.assertEqual(summary["p95"], 95.0)
        self.assertEqual(summary["p99"], 99.0)
        self.assertEqual(summary["max"], 100.0)
        self.assertEqual(summary["mean"], 50.5)

    def test_unsorted_input_is_sorted_and_not_mutated(self) -> None:
        samples = [3.0, 1.0, 2.0, 4.0]
        summary = percentiles(samples)
        self.assertEqual(summary["p50"], 2.0)
        self.assertEqual(summary["p95"], 4.0)
        self.assertEqual(summary["min"], 1.0)
        self.assertEqual(samples, [3.0, 1.0, 2.0, 4.0])

    def test_values_are_rounded_to_three_decimals(self) -> None:
        summary = percentiles([1.23456, 2.34567])
        self.assertEqual(summary["min"], 1.235)
        self.assertEqual(summary["max"], 2.346)
        self.assertEqual(summary["mean"], 1.79)
        for key in ("min", "p50", "p95", "p99", "max", "mean"):
            self.assertIsInstance(summary[key], float)


class ContextCostTest(unittest.TestCase):
    def test_counts_bytes_and_tokens(self) -> None:
        tools = [
            {"name": "small", "description": "x"},
            {"name": "large", "description": "y" * 400},
        ]
        cost = context_cost(tools)
        expected_bytes = len(
            json.dumps(tools, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        self.assertEqual(cost["tools"], 2)
        self.assertEqual(cost["tools_list_bytes"], expected_bytes)
        self.assertEqual(cost["approx_tokens"], int(round(expected_bytes / 4)))
        self.assertIn("approximation", cost["method"])

    def test_largest_tools_sorted_descending_and_capped(self) -> None:
        tools = [
            {"name": f"tool_{index}", "description": "z" * (index * 40)}
            for index in range(7)
        ]
        cost = context_cost(tools)
        largest = cost["largest_tools"]
        self.assertEqual(len(largest), 5)
        self.assertEqual([entry["name"] for entry in largest][0], "tool_6")
        sizes = [entry["bytes"] for entry in largest]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(largest[0]["approx_tokens"], int(round(largest[0]["bytes"] / 4)))

    def test_empty_catalog_reports_empty_array(self) -> None:
        cost = context_cost([])
        self.assertEqual(cost["tools"], 0)
        self.assertEqual(cost["tools_list_bytes"], len("[]"))
        self.assertEqual(cost["largest_tools"], [])


class SpawnedLoadTest(unittest.TestCase):
    def test_small_spawn_run_is_clean_and_closes_port(self) -> None:
        report = run_load(None, clients=4, requests=5, tool="account_summary", args={}, timeout=10)
        self.assertEqual(report["benchmark"], "x-starter-http-load")
        self.assertEqual(report["transport"], "streamable-http")
        self.assertEqual(report["mode"], "mock")
        self.assertEqual(report["total_requests"], 20)
        self.assertEqual(report["errors"]["total"], 0)
        self.assertEqual(report["errors"]["rate"], 0.0)
        self.assertEqual(report["errors"]["by_kind"], {})
        self.assertEqual(report["sessions_closed"], 4)
        self.assertEqual(report["latency_ms"]["handshake"]["count"], 4)
        self.assertEqual(report["latency_ms"]["tools_list"]["count"], 12)
        self.assertEqual(report["latency_ms"]["tools_call"]["count"], 8)
        self.assertEqual(report["context_cost"]["tools"], len(XStarter(mode="mock", environ={}).tool_definitions()))
        self.assertGreater(report["throughput_rps"], 0)
        self.assertEqual(report["connections"], report["connect_ms"]["count"])
        self.assertGreaterEqual(report["connections"], 4)
        # Finite SSE bodies carry Content-Length, so each client keeps one
        # keep-alive connection for its whole run.
        self.assertEqual(report["connections"], report["clients"])

    def test_spawned_listener_is_closed_after_run(self) -> None:
        report = run_load(None, clients=2, requests=2, timeout=10)
        self.assertEqual(report["errors"]["total"], 0)
        self.assertTrue(
            _port_closed(report["target_url"]),
            f"port still accepting connections at {report['target_url']}",
        )

    def test_unknown_tool_is_counted_as_error(self) -> None:
        report = run_load(
            None, clients=3, requests=4, tool="__missing_tool__", args={}, timeout=10
        )
        self.assertEqual(report["total_requests"], 12)
        self.assertEqual(report["errors"]["total"], 6)
        kinds = report["errors"]["by_kind"]
        self.assertTrue(
            set(kinds) <= {"tool_error"} or any(kind.startswith("jsonrpc_") for kind in kinds),
            f"unexpected error kinds: {sorted(kinds)}",
        )
        self.assertEqual(report["sessions_closed"], 3)
        self.assertGreater(report["errors"]["rate"], 0)


if __name__ == "__main__":
    unittest.main()
