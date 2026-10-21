"""Tests for the offline connector acceptance matrix.

Run from the x-starter directory:

    python -X utf8 -m unittest tests.test_connector_e2e -v
"""

from __future__ import annotations

import copy
import json
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import connector_e2e  # noqa: E402
from connector_e2e import (  # noqa: E402
    NATIVE_CONNECTOR_ID,
    REPORT_SCHEMA,
    default_probe,
    dumps_report,
    run_contract_matrix,
)


def native_descriptor() -> dict:
    return {
        "connector_id": "x-native",
        "title": "X Starter (local mock)",
        "version": "0.3.0",
        "kind": "mcp",
        "endpoint": "http://127.0.0.1:8765/mcp",
        "auth": "host",
        "status": "available",
        "read_only_default": True,
        "operations": [
            {"name": "read_timeline", "read_only": True},
            {"name": "suggest_post", "read_only": True},
        ],
    }


def official_descriptor() -> dict:
    return {
        "connector_id": "x-official-hosted",
        "title": "Official X MCP (optional)",
        "version": "0.1.0",
        "kind": "mcp",
        "endpoint": "https://api.x.com/mcp",
        "auth": "oauth",
        "status": "optional",
        "read_only_default": True,
        "operations": [{"name": "search_posts", "read_only": True}],
        "metadata": {"note": "Not connected; descriptor only."},
    }


def web_intent_descriptor() -> dict:
    return {
        "connector_id": "web-intent",
        "title": "Web intent fallback",
        "version": "0.3.0",
        "kind": "web_intent",
        "endpoint": "https://x.com/intent/post",
        "auth": "user",
        "status": "available",
        "read_only_default": True,
        "operations": [{"name": "handoff_to_browser", "read_only": True}],
    }


def by_id(report: dict, connector_id: str) -> dict:
    for item in report["results"]:
        if item["connector_id"] == connector_id:
            return item
    raise AssertionError(f"missing connector result: {connector_id}")


class NativeConnectorTest(unittest.TestCase):
    def test_native_mock_passes_without_probe(self) -> None:
        report = run_contract_matrix([native_descriptor()])
        result = by_id(report, NATIVE_CONNECTOR_ID)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["passed"], len(result["checks"]))

    def test_native_result_names_every_check(self) -> None:
        report = run_contract_matrix([native_descriptor()])
        names = {item["check"] for item in by_id(report, NATIVE_CONNECTOR_ID)["checks"]}
        self.assertIn("descriptor_schema", names)
        self.assertIn("endpoint_format", names)
        self.assertIn("auth_value", names)
        self.assertIn("status_value", names)
        self.assertIn("operations_shape", names)
        self.assertIn("read_only_default_type", names)
        self.assertIn("connection_probe", names)


class ExternalConnectorTest(unittest.TestCase):
    def test_official_hosted_skips_without_probe(self) -> None:
        report = run_contract_matrix([official_descriptor()])
        result = by_id(report, "x-official-hosted")
        self.assertEqual(result["status"], "skip")
        self.assertEqual(result["failed"], 0)
        self.assertGreaterEqual(result["skipped"], 1)
        self.assertFalse(
            any(item["status"] == "pass" for item in result["checks"] if item["check"] == "connection_probe")
        )

    def test_skip_detail_never_claims_a_connection(self) -> None:
        report = run_contract_matrix([official_descriptor()])
        probe_checks = [
            item
            for item in by_id(report, "x-official-hosted")["checks"]
            if item["check"] == "connection_probe"
        ]
        self.assertEqual(len(probe_checks), 1)
        self.assertIn("explicit probe", probe_checks[0]["detail"])

    def test_web_intent_skips_without_probe(self) -> None:
        report = run_contract_matrix([web_intent_descriptor()])
        self.assertEqual(by_id(report, "web-intent")["status"], "skip")

    def test_external_probe_that_passes_allows_pass(self) -> None:
        calls: list[str] = []

        def probe(descriptor: dict) -> dict:
            calls.append(descriptor["connector_id"])
            return {"ok": True, "checks": [{"check": "http_reachable", "status": "pass", "detail": "stubbed"}]}

        report = run_contract_matrix([official_descriptor()], probe=probe)
        result = by_id(report, "x-official-hosted")
        self.assertEqual(calls, ["x-official-hosted"])
        self.assertEqual(result["status"], "pass")
        self.assertIn("http_reachable", {item["check"] for item in result["checks"]})

    def test_probe_exception_fails_instead_of_skips(self) -> None:
        def probe(descriptor: dict) -> dict:
            raise TimeoutError("upstream did not answer")

        report = run_contract_matrix([official_descriptor()], probe=probe)
        result = by_id(report, "x-official-hosted")
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["failed"], 1)
        detail = [item for item in result["checks"] if item["check"] == "probe_exception"][0]["detail"]
        self.assertIn("TimeoutError", detail)

    def test_probe_exception_message_is_not_echoed(self) -> None:
        def probe(descriptor: dict) -> dict:
            raise RuntimeError("bearer TOKEN-MARKER-12345")

        report = run_contract_matrix([official_descriptor()], probe=probe)
        self.assertNotIn("TOKEN-MARKER-12345", dumps_report(report))

    def test_probe_returning_non_dict_fails(self) -> None:
        report = run_contract_matrix([official_descriptor()], probe=lambda _d: "ok")
        self.assertEqual(by_id(report, "x-official-hosted")["status"], "fail")

    def test_probe_returning_bad_check_status_fails(self) -> None:
        def probe(descriptor: dict) -> dict:
            return {"ok": True, "checks": [{"check": "x", "status": "maybe", "detail": ""}]}

        report = run_contract_matrix([official_descriptor()], probe=probe)
        self.assertEqual(by_id(report, "x-official-hosted")["status"], "fail")

    def test_probe_contradiction_fails(self) -> None:
        def probe(descriptor: dict) -> dict:
            return {"ok": False, "checks": [{"check": "x", "status": "pass", "detail": "fine"}]}

        report = run_contract_matrix([official_descriptor()], probe=probe)
        self.assertEqual(by_id(report, "x-official-hosted")["status"], "fail")

    def test_probe_receives_a_copy(self) -> None:
        original = official_descriptor()
        frozen = copy.deepcopy(original)

        def probe(descriptor: dict) -> dict:
            descriptor["connector_id"] = "mutated"
            descriptor["metadata"]["note"] = "mutated"
            return {"ok": True, "checks": []}

        run_contract_matrix([original], probe=probe)
        self.assertEqual(original, frozen)

    def test_probe_must_be_callable(self) -> None:
        with self.assertRaises(TypeError):
            run_contract_matrix([official_descriptor()], probe="probe")

    def test_descriptors_must_be_a_list(self) -> None:
        with self.assertRaises(TypeError):
            run_contract_matrix("not-a-list")  # type: ignore[arg-type]


class InvalidDescriptorTest(unittest.TestCase):
    def test_missing_fields_fail(self) -> None:
        report = run_contract_matrix([{"connector_id": "broken"}])
        result = by_id(report, "broken")
        self.assertEqual(result["status"], "fail")
        missing_check = [item for item in result["checks"] if item["check"] == "required_fields"][0]
        self.assertEqual(missing_check["status"], "fail")
        self.assertIn("operations", missing_check["missing"])

    def test_non_dict_descriptor_fails_with_placeholder_id(self) -> None:
        report = run_contract_matrix(["nope"])  # type: ignore[list-item]
        self.assertEqual(report["results"][0]["connector_id"], "<descriptor[0]>")
        self.assertEqual(report["results"][0]["status"], "fail")

    def test_bad_connector_id_fails(self) -> None:
        descriptor = native_descriptor()
        descriptor["connector_id"] = "bad id!"
        report = run_contract_matrix([descriptor])
        checks = {item["check"]: item["status"] for item in report["results"][0]["checks"]}
        self.assertEqual(checks["connector_id_format"], "fail")

    def test_bad_kind_fails(self) -> None:
        descriptor = native_descriptor()
        descriptor["kind"] = "carrier_pigeon"
        report = run_contract_matrix([descriptor])
        self.assertEqual(report["results"][0]["status"], "fail")

    def test_remote_plain_http_fails(self) -> None:
        descriptor = official_descriptor()
        descriptor["endpoint"] = "http://api.x.com/mcp"
        report = run_contract_matrix([descriptor])
        checks = {item["check"]: item["status"] for item in report["results"][0]["checks"]}
        self.assertEqual(checks["endpoint_format"], "fail")

    def test_endpoint_with_embedded_credentials_fails(self) -> None:
        descriptor = official_descriptor()
        descriptor["endpoint"] = "https://user:pass@api.x.com/mcp"
        report = run_contract_matrix([descriptor])
        self.assertEqual(report["results"][0]["status"], "fail")

    def test_empty_operations_fail(self) -> None:
        descriptor = native_descriptor()
        descriptor["operations"] = []
        report = run_contract_matrix([descriptor])
        checks = {item["check"]: item["status"] for item in report["results"][0]["checks"]}
        self.assertEqual(checks["operations_shape"], "fail")

    def test_duplicate_operation_names_fail(self) -> None:
        descriptor = native_descriptor()
        descriptor["operations"] = [{"name": "read", "read_only": True}] * 2
        report = run_contract_matrix([descriptor])
        checks = {item["check"]: item["status"] for item in report["results"][0]["checks"]}
        self.assertEqual(checks["operation_names_unique"], "fail")

    def test_write_without_confirmation_fails(self) -> None:
        descriptor = native_descriptor()
        descriptor["operations"] = [
            {"name": "publish", "read_only": False, "requires_confirmation": False}
        ]
        report = run_contract_matrix([descriptor])
        checks = {item["check"]: item["status"] for item in report["results"][0]["checks"]}
        self.assertEqual(checks["operation_write_confirmation"], "fail")

    def test_credential_shaped_key_fails(self) -> None:
        descriptor = native_descriptor()
        descriptor["metadata"] = {"client_secret": "must-not-live-here"}
        report = run_contract_matrix([descriptor])
        self.assertEqual(report["results"][0]["status"], "fail")
        self.assertNotIn("must-not-live-here", dumps_report(report))

    def test_bad_auth_fails(self) -> None:
        descriptor = native_descriptor()
        descriptor["auth"] = "magic"
        report = run_contract_matrix([descriptor])
        self.assertEqual(report["results"][0]["status"], "fail")

    def test_oauth_without_endpoint_fails(self) -> None:
        descriptor = official_descriptor()
        descriptor["endpoint"] = None
        report = run_contract_matrix([descriptor])
        checks = {item["check"]: item["status"] for item in report["results"][0]["checks"]}
        self.assertEqual(checks["auth_endpoint"], "fail")

    def test_disabled_connector_skips(self) -> None:
        descriptor = official_descriptor()
        descriptor["status"] = "disabled"
        report = run_contract_matrix([descriptor])
        result = by_id(report, "x-official-hosted")
        self.assertEqual(result["status"], "skip")
        self.assertIn(
            "disabled",
            [item for item in result["checks"] if item["check"] == "connector_runnable"][0]["detail"],
        )


class AggregateAndStabilityTest(unittest.TestCase):
    def test_multiple_connectors_are_summarized(self) -> None:
        report = run_contract_matrix(
            [native_descriptor(), official_descriptor(), web_intent_descriptor()]
        )
        totals = report["totals"]
        self.assertEqual(totals["connectors"], 3)
        self.assertEqual(totals["passed"], 1)
        self.assertEqual(totals["failed"], 0)
        self.assertEqual(totals["skipped"], 2)
        self.assertEqual(totals["status"], "skip")

    def test_failures_dominate_the_aggregate_status(self) -> None:
        report = run_contract_matrix([native_descriptor(), {"connector_id": "broken"}])
        self.assertEqual(report["totals"]["status"], "fail")
        self.assertEqual(report["totals"]["failed"], 1)
        self.assertGreaterEqual(report["totals"]["checks"]["failed"], 1)

    def test_empty_input_is_a_pass_with_no_connectors(self) -> None:
        report = run_contract_matrix([])
        self.assertEqual(report["results"], [])
        self.assertEqual(report["totals"]["connectors"], 0)
        self.assertEqual(report["totals"]["status"], "pass")

    def test_report_declares_zero_network_calls(self) -> None:
        report = run_contract_matrix([native_descriptor(), official_descriptor()])
        self.assertEqual(report["network_calls"], 0)
        self.assertEqual(report["schema"], REPORT_SCHEMA)

    def test_matrix_opens_no_sockets(self) -> None:
        def forbidden(*args, **kwargs):
            raise AssertionError("the matrix must not open a socket")

        with mock.patch.object(socket, "socket", forbidden), mock.patch.object(
            socket, "create_connection", forbidden
        ):
            report = run_contract_matrix(
                [native_descriptor(), official_descriptor(), web_intent_descriptor()]
            )
        self.assertEqual(report["network_calls"], 0)

    def test_acceptance_section_states_limits(self) -> None:
        report = run_contract_matrix([native_descriptor()])
        acceptance = report["acceptance"]
        self.assertIn("criteria", acceptance)
        joined = " ".join(acceptance["not_covered"]).lower()
        self.assertIn("oauth", joined)
        self.assertIn("multi-tenant", joined)
        self.assertIn("official mcp", joined)

    def test_report_is_json_stable_across_runs(self) -> None:
        descriptors = [native_descriptor(), official_descriptor()]
        first = run_contract_matrix(descriptors)
        second = run_contract_matrix(copy.deepcopy(descriptors))
        self.assertEqual(first, second)
        self.assertEqual(dumps_report(first), dumps_report(second))
        self.assertEqual(json.loads(dumps_report(first)), first)

    def test_report_keys_are_sorted_in_serialized_form(self) -> None:
        report = run_contract_matrix([native_descriptor()])
        text = dumps_report(report)
        self.assertTrue(text.startswith('{"acceptance":'))
        self.assertNotIn("\n", text)

    def test_returned_report_is_mutable_without_side_effects(self) -> None:
        descriptor = native_descriptor()
        first = run_contract_matrix([descriptor])
        first["results"][0]["checks"].append({"check": "injected", "status": "fail", "detail": ""})
        second = run_contract_matrix([descriptor])
        self.assertEqual(second["results"][0]["status"], "pass")
        self.assertNotIn(
            "injected", {item["check"] for item in second["results"][0]["checks"]}
        )
        self.assertEqual(descriptor, native_descriptor())

    def test_default_probe_reports_ok_flag(self) -> None:
        self.assertTrue(default_probe(native_descriptor())["ok"])
        broken = default_probe({"connector_id": "broken"})
        self.assertFalse(broken["ok"])
        self.assertTrue(any(item["status"] == "fail" for item in broken["checks"]))

    def test_module_exports_are_ascii(self) -> None:
        text = Path(connector_e2e.__file__).read_text(encoding="utf-8")
        self.assertTrue(text.isascii())


if __name__ == "__main__":
    unittest.main()
