"""Tests for the public installation contract.

The contract has two files:

- ``public_skill.md``: what an agent reads before its first call.
- ``connector-manifest.json``: which connectors exist and how to plan a call.

These tests are offline. They read the two files and compare them with the
connector runtime and the server tool catalog. They never open a socket and
never read a credential file.

Run from the x-starter directory:

    python -X utf8 -m unittest tests.test_public_contract -v
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MANIFEST_PATH = ROOT / "connector-manifest.json"
SKILL_PATH = ROOT / "public_skill.md"
SERVER_PATH = ROOT / "server.py"

PLACEHOLDER_URL = "https://YOUR_AHAN_HOST/mcp"
REQUIRED_TOP_LEVEL = (
    "schema",
    "schema_name",
    "schema_version",
    "service",
    "mcp",
    "connectors",
    "execution_modes",
    "workflow",
    "extending",
    "boundaries",
)
REQUIRED_CONNECTOR_FIELDS = (
    "id",
    "connector_id",
    "title",
    "version",
    "kind",
    "transport",
    "endpoint",
    "auth",
    "status",
    "read_only_default",
    "operations",
)
REQUIRED_IDS = ("x-native", "x-official-hosted", "web-intent", "computer-use")
MODES = ("mcp", "api", "browser", "web_intent", "draft_only")
KINDS = ("mcp", "api", "browser", "web_intent")
STATUSES = ("available", "degraded", "disabled", "optional")
AUTH_MODES = ("none", "oauth", "host", "user")
SIDE_EFFECTS = ("none", "local", "external")
VERIFICATION_SCOPES = (
    "mock-only",
    "descriptor-only",
    "offline-format-only",
    "gated-local",
    "live-public-read",
)
# The runtime ranks three statuses. The manifest also allows "optional", which
# an adapter maps to "degraded" until a live check passes.
RUNTIME_STATUS_ALIASES = {"optional": "degraded"}

SECRET_KEY_PARTS = ("token", "secret", "password", "cookie", "authorization", "bearer")
SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9]{8,}"),
    re.compile(r"AAAAAAAAAAAAAAAAAAAA[A-Za-z0-9%]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def load_skill() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def connectors_by_id(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in manifest["connectors"]}


def walk(value: Any, path: str = "$") -> list[tuple[str, Any]]:
    """Return every (dotted path, value) pair in a nested JSON value."""

    found: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            found.append((f"{path}.{key}", child))
            found.extend(walk(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.append((f"{path}[{index}]", child))
            found.extend(walk(child, f"{path}[{index}]"))
    return found


class ManifestShapeTests(unittest.TestCase):
    """The manifest must be readable before any connector is registered."""

    def test_manifest_has_required_sections(self) -> None:
        manifest = load_manifest()
        self.assertIsInstance(manifest, dict)
        for key in REQUIRED_TOP_LEVEL:
            self.assertIn(key, manifest, f"missing top-level field {key!r}")

    def test_schema_is_v1(self) -> None:
        manifest = load_manifest()
        self.assertEqual(manifest["schema"], "v1")
        self.assertEqual(manifest["schema_name"], "ahan-connector-manifest")
        self.assertEqual(manifest["schema_version"], 1)
        self.assertIsInstance(manifest["schema_version"], int)

    def test_service_describes_a_connector_gateway(self) -> None:
        service = load_manifest()["service"]
        self.assertEqual(service["name"], "ahan")
        self.assertEqual(service["role"], "pluggable-connector-gateway")
        self.assertTrue(service["version"])
        self.assertTrue(service["summary"])
        self.assertEqual(service["entrypoint_doc"], "public_skill.md")

    def test_mcp_block_uses_placeholder_url_and_call_order(self) -> None:
        block = load_manifest()["mcp"]
        self.assertEqual(block["url"], PLACEHOLDER_URL)
        self.assertTrue(block["url_is_placeholder"])
        self.assertEqual(block["transport"], "streamable-http")
        self.assertEqual(block["call_order"], ["initialize", "tools/list", "tools/call"])
        self.assertFalse(block["auth"]["in_band_credentials"])
        self.assertIn("tools/list", block["discovery_rule"])

    def test_connectors_have_every_required_field(self) -> None:
        manifest = load_manifest()
        self.assertGreaterEqual(len(manifest["connectors"]), 4)
        declared = set(manifest["extending"]["required_fields"])
        self.assertTrue(set(REQUIRED_CONNECTOR_FIELDS) <= declared)
        for connector in manifest["connectors"]:
            for field in REQUIRED_CONNECTOR_FIELDS:
                self.assertIn(field, connector, f"{connector.get('id')} lacks {field!r}")
            self.assertIsInstance(connector["operations"], list)
            self.assertTrue(connector["operations"], f"{connector['id']} lists no operations")
            self.assertIn(connector["verification_scope"], VERIFICATION_SCOPES)

    def test_connector_ids_are_unique_and_known(self) -> None:
        connectors = load_manifest()["connectors"]
        ids = [item["id"] for item in connectors]
        self.assertEqual(len(ids), len(set(ids)), "connector ids must be unique")
        for required in REQUIRED_IDS:
            self.assertIn(required, ids)

    def test_connector_id_duplicates_the_acceptance_matrix_key(self) -> None:
        """The e2e matrix reads connector_id; the manifest also uses id."""

        for connector in load_manifest()["connectors"]:
            self.assertEqual(
                connector["connector_id"],
                connector["id"],
                "connector_id and id must name the same connector",
            )

    def test_enumerations_match_the_connector_runtime(self) -> None:
        from connector_runtime import KNOWN_AUTH, KNOWN_KINDS, KNOWN_STATUS

        for connector in load_manifest()["connectors"]:
            self.assertIn(connector["kind"], KINDS)
            self.assertIn(connector["kind"], KNOWN_KINDS)
            self.assertIn(connector["status"], STATUSES)
            runtime_status = RUNTIME_STATUS_ALIASES.get(
                connector["status"], connector["status"]
            )
            self.assertIn(runtime_status, KNOWN_STATUS)
            self.assertIn(connector["auth"], AUTH_MODES)
            self.assertIn(connector["auth"], KNOWN_AUTH)
            for operation in connector["operations"]:
                self.assertIn(operation["execution_mode"], MODES)
                self.assertIn(operation["side_effect"], SIDE_EFFECTS)

    def test_optional_status_is_documented_as_an_alias(self) -> None:
        block = load_manifest()["mcp"]
        self.assertIn("optional", block["status_note"])
        self.assertIn("degraded", block["status_note"])

    def test_operation_fields_are_documented(self) -> None:
        fields = load_manifest()["operation_fields"]
        for name in ("read_only", "side_effect", "requires_confirmation", "execution_mode"):
            self.assertIn(name, fields)

    def test_declared_execution_modes_cover_the_ladder(self) -> None:
        modes = [item["mode"] for item in load_manifest()["execution_modes"]]
        self.assertEqual(sorted(modes), sorted(MODES))
        for mode in ("api", "browser", "web_intent", "draft_only"):
            self.assertIn(mode, modes)

    def test_execution_mode_ranks_follow_the_runtime_order(self) -> None:
        from connector_runtime import DEFAULT_MODE_ORDER

        entries = sorted(
            load_manifest()["execution_modes"], key=lambda item: item["rank"]
        )
        ranks = [item["rank"] for item in entries]
        self.assertEqual(ranks, sorted(ranks), "ranks must be unique and ascending")
        ranked = tuple(item["mode"] for item in entries if item["mode"] != "draft_only")
        self.assertEqual(ranked, DEFAULT_MODE_ORDER)

    def test_every_non_draft_mode_requires_confirmation_for_writes(self) -> None:
        for entry in load_manifest()["execution_modes"]:
            if entry["mode"] == "draft_only":
                self.assertFalse(entry["write_requires_confirmation"])
            else:
                self.assertTrue(
                    entry["write_requires_confirmation"],
                    f"{entry['mode']} allows a write without confirmation",
                )
            self.assertIn("requires", entry)


class ConnectorStatusTests(unittest.TestCase):
    """An unverified connector must not look connected."""

    def test_official_hosted_connector_is_optional_and_user_authorized(self) -> None:
        official = connectors_by_id(load_manifest())["x-official-hosted"]
        self.assertEqual(official["status"], "optional")
        self.assertEqual(official["auth"], "user")
        self.assertTrue(official["read_only_default"])
        self.assertFalse(official["live_connection_verified"])
        self.assertEqual(official["verification_scope"], "descriptor-only")
        self.assertTrue(official["requires_owner_consent"])

    def test_official_connector_does_not_claim_post_creation(self) -> None:
        official = connectors_by_id(load_manifest())["x-official-hosted"]
        names = {item["name"] for item in official["operations"]}
        for write_name in ("create_post", "publish_post", "post_tweet", "delete_post"):
            self.assertNotIn(write_name, names)
        self.assertTrue(all(item["read_only"] for item in official["operations"]))
        joined = " ".join(official["limitations"]).lower()
        self.assertIn("not connected", joined)

    def test_native_connector_is_marked_mock_only(self) -> None:
        native = connectors_by_id(load_manifest())["x-native"]
        self.assertEqual(native["status"], "available")
        self.assertFalse(native["live_connection_verified"])
        self.assertEqual(native["verification_scope"], "mock-only")
        # Every published endpoint is the same placeholder, so no real host
        # leaks into a file that ships with the service.
        self.assertEqual(native["endpoint"], PLACEHOLDER_URL)
        self.assertEqual(load_manifest()["mcp"]["url"], PLACEHOLDER_URL)

    def test_a_verified_flag_needs_a_live_scope_and_evidence(self) -> None:
        """A live claim must name the check that produced it.

        Unverified connectors stay false. The one exception is a connector whose
        verification scope is ``live-public-read``, which must also name the
        account and date the read was made against.
        """

        for connector in load_manifest()["connectors"]:
            if not connector["live_connection_verified"]:
                continue
            self.assertEqual(
                connector["verification_scope"],
                "live-public-read",
                f"{connector['id']} claims a live connection without a live scope",
            )
            evidence = " ".join(connector.get("limitations", []))
            self.assertIn("Verified live against", evidence)

    def test_read_only_never_means_an_external_change(self) -> None:
        for connector in load_manifest()["connectors"]:
            for operation in connector["operations"]:
                label = f"{connector['id']}.{operation['name']}"
                if operation["read_only"]:
                    self.assertNotEqual(operation["side_effect"], "external", label)
                else:
                    self.assertEqual(operation["side_effect"], "external", label)
                if operation["side_effect"] == "none":
                    self.assertFalse(operation["requires_confirmation"], label)

    def test_every_external_side_effect_requires_confirmation(self) -> None:
        external = 0
        for connector in load_manifest()["connectors"]:
            for operation in connector["operations"]:
                if operation["side_effect"] != "external":
                    continue
                external += 1
                self.assertTrue(
                    operation["requires_confirmation"],
                    f"{connector['id']}.{operation['name']} changes X without confirmation",
                )
                self.assertFalse(operation["read_only"])
        self.assertGreater(external, 0, "the manifest declares no external write")

    def test_connector_confirmation_flag_matches_its_operations(self) -> None:
        for connector in load_manifest()["connectors"]:
            expected = any(
                operation["side_effect"] == "external"
                for operation in connector["operations"]
            )
            self.assertEqual(
                connector["requires_confirmation"],
                expected,
                f"{connector['id']} has an inconsistent requires_confirmation flag",
            )

    def test_read_only_default_matches_the_operation_set(self) -> None:
        for connector in load_manifest()["connectors"]:
            expected = all(
                operation["side_effect"] == "none"
                for operation in connector["operations"]
            )
            self.assertEqual(
                connector["read_only_default"],
                expected,
                f"{connector['id']} has an inconsistent read_only_default flag",
            )

    def test_local_side_effect_is_labelled_as_local(self) -> None:
        native = connectors_by_id(load_manifest())["x-native"]
        prepare = next(
            item for item in native["operations"] if item["name"] == "x_post_prepare"
        )
        self.assertEqual(prepare["side_effect"], "local")
        self.assertTrue(prepare["read_only"])
        self.assertFalse(prepare["requires_confirmation"])


class SecretScanTests(unittest.TestCase):
    """The public contract must be safe to publish as-is."""

    def test_no_credential_shaped_keys(self) -> None:
        offenders: list[str] = []
        for path, _ in walk(load_manifest()):
            leaf = path.rsplit(".", 1)[-1].split("[")[0].lower()
            if leaf.endswith(("_included", "_verified", "_default", "_mode", "_credentials")):
                continue
            if any(part in leaf for part in SECRET_KEY_PARTS):
                offenders.append(path)
        self.assertEqual(offenders, [])

    def test_no_credential_shaped_values(self) -> None:
        text = MANIFEST_PATH.read_text(encoding="utf-8") + SKILL_PATH.read_text(
            encoding="utf-8"
        )
        for pattern in SECRET_VALUE_PATTERNS:
            match = pattern.search(text)
            if match is not None:
                self.fail(f"possible secret value: {match.group(0)[:12]}...")

    def test_every_endpoint_is_public_or_a_placeholder(self) -> None:
        manifest = load_manifest()
        allowed_hosts = {"YOUR_AHAN_HOST", "api.x.com", "x.com", "127.0.0.1", "localhost"}
        endpoints = [manifest["mcp"]["url"]]
        endpoints.extend(
            item["endpoint"]
            for item in manifest["connectors"]
            if isinstance(item.get("endpoint"), str)
        )
        for endpoint in endpoints:
            host = endpoint.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0]
            self.assertIn(host, allowed_hosts, f"unexpected host in {endpoint!r}")
            self.assertNotIn("@", endpoint, f"endpoint embeds user info: {endpoint!r}")

    def test_skill_file_contains_no_absolute_paths(self) -> None:
        text = load_skill()
        self.assertNotRegex(text, r"(?i)\b[a-z]:\\")
        self.assertNotRegex(text, r"/Users/|/home/")


class SkillDocumentTests(unittest.TestCase):
    """The document must teach discovery, negotiation, and honest outcomes."""

    def test_skill_has_frontmatter_and_manifest_pointer(self) -> None:
        text = load_skill()
        self.assertTrue(text.startswith("---\n"))
        header = text.split("---", 2)[1]
        self.assertIn("name: ahan-connector-gateway", header)
        self.assertIn("manifest: ./connector-manifest.json", header)

    def test_skill_lists_every_connector_once(self) -> None:
        text = load_skill()
        for connector in load_manifest()["connectors"]:
            marker = f"| `{connector['id']}` |"
            self.assertEqual(
                text.count(marker),
                1,
                f"{connector['id']} should appear exactly once in the connector table",
            )

    def test_skill_does_not_call_optional_connectors_connected(self) -> None:
        text = load_skill()
        section = text.split("## Connectors in this manifest", 1)[1].split("##", 1)[0]
        for entry in load_manifest()["connectors"]:
            if entry["status"] != "optional":
                continue
            line = next(
                row for row in section.splitlines() if f"`{entry['id']}`" in row
            )
            self.assertIn("optional", line)
            lowered = line.lower()
            self.assertNotIn("connected |", lowered.replace("not connected", ""))

    def test_skill_requires_tools_list_before_call(self) -> None:
        text = load_skill()
        listed = text.index("Call `tools/list` first")
        called = text.index("Call `tools/call`")
        self.assertLess(listed, called)
        self.assertIn("UNKNOWN_TOOL", text)

    def test_skill_documents_capability_negotiation(self) -> None:
        text = load_skill()
        self.assertIn("## Capability negotiation", text)
        for field in (
            "api",
            "computer_use",
            "web_intent",
            "mcp_apps",
            "mcp_connectors",
            "execution_modes",
        ):
            self.assertIn(field, text)
        self.assertIn("documented defaults apply", text)

    def test_skill_documents_all_execution_modes(self) -> None:
        text = load_skill()
        self.assertIn("## Execution modes", text)
        for mode in MODES:
            self.assertIn(f"`{mode}`", text)
        self.assertIn("degradation ladder", text.lower())

    def test_skill_documents_the_full_workflow_in_order(self) -> None:
        text = load_skill()
        steps = ("Read timeline", "Filter", "Suggest", "Confirm", "Execute", "Verify")
        positions = [text.index(f"**{step}.**") for step in steps]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("x_timeline_observe", text)
        self.assertIn("x_post_prepare", text)
        self.assertIn("x_post_publish", text)
        self.assertIn("x_timeline_restore", text)

    def test_skill_documents_uncertain_rules(self) -> None:
        text = load_skill()
        self.assertIn("## Uncertain results", text)
        self.assertIn("Do not retry the write", text)
        self.assertIn("Never turn an unknown into a success claim", text)

    def test_skill_documents_first_login_and_batch_boundary(self) -> None:
        text = load_skill()
        self.assertIn("## First-login handoff", text)
        self.assertIn("Continue with Google", text)
        self.assertIn("x_interaction_batch_plan", text)
        self.assertIn("one_minute_sla: not_verified", text)
        self.assertIn("x_interaction_batch_prepare", text)
        self.assertIn("x_interaction_batch_start", text)
        self.assertIn("x_interaction_batch_execute", text)
        self.assertIn("x_interaction_batch_status", text)

    def test_skill_documents_connector_extension_rules(self) -> None:
        text = load_skill()
        self.assertIn("## Add a connector", text)
        self.assertIn("live_connection_verified", text)
        self.assertIn("offline contract matrix", text)
        self.assertIn("side_effect", text)

    def test_skill_states_unverified_boundaries(self) -> None:
        text = load_skill()
        self.assertIn("## What this skill does not claim", text)
        self.assertIn("does not prove a live X API connection", text)
        self.assertIn("OAuth grant", text)

    def test_documented_error_codes_exist_in_the_source(self) -> None:
        """A documented code that no module raises is a dead instruction."""

        text = load_skill()
        section = text.split("## Errors", 1)[1].split("\n## ", 1)[0]
        codes = re.findall(r"^\| `([A-Z_]{4,})` \|", section, flags=re.MULTILINE)
        self.assertGreaterEqual(len(codes), 10, "the error table is too short")
        sources = "\n".join(
            (ROOT / name).read_text(encoding="utf-8")
            for name in (
                "server.py",
                "write_store.py",
                "browser_bridge.py",
                "x_api.py",
                "oauth.py",
                "x_public.py",
                "agent_help_board.py",
            )
        )
        for code in codes:
            self.assertIn(code, sources, f"{code} is documented but never raised")


class RuntimeIntegrationTests(unittest.TestCase):
    """The manifest must load into the runtime and match the server catalog."""

    def test_manifest_builds_a_runtime_registry(self) -> None:
        from connector_runtime import (
            ConnectorCapability,
            ConnectorDescriptor,
            ConnectorRegistry,
        )

        manifest = load_manifest()
        registry = ConnectorRegistry()
        for item in manifest["connectors"]:
            capabilities = tuple(
                ConnectorCapability(
                    name=operation["name"],
                    kind=operation["execution_mode"],
                    operations=(operation["name"],),
                    auth=item["auth"],
                    read_only=operation["read_only"],
                )
                for operation in item["operations"]
            )
            registry.register(
                ConnectorDescriptor(
                    connector_id=item["id"],
                    title=item["title"],
                    version=item["version"],
                    endpoint=item["endpoint"],
                    capabilities=capabilities,
                    status="degraded" if item["status"] == "optional" else item["status"],
                )
            )
        self.assertEqual(len(registry.list()), len(manifest["connectors"]))
        ranked = registry.select(operation="x_timeline_observe")
        self.assertTrue(ranked)
        self.assertEqual(ranked[0].connector_id, "x-native")

    def test_manifest_capability_defaults_match_the_runtime(self) -> None:
        from connector_runtime import ConnectorRegistry

        negotiated = ConnectorRegistry().negotiate({})
        defaults = load_manifest()["mcp"]["capability_negotiation"]["defaults"]
        self.assertEqual(negotiated["execution_modes"][0], "mcp")
        self.assertEqual(negotiated["api"], defaults["api"])
        self.assertEqual(negotiated["computer_use"], defaults["computer_use"])
        self.assertEqual(negotiated["web_intent"], defaults["web_intent"])
        self.assertEqual(negotiated["mcp_apps"], defaults["mcp_apps"])
        self.assertTrue(defaults["mcp_connectors"])
        self.assertEqual(negotiated["execution_modes"][-1], "draft_only")

    def test_declared_native_tools_exist_in_the_server_catalog(self) -> None:
        source = SERVER_PATH.read_text(encoding="utf-8")
        native = connectors_by_id(load_manifest())["x-native"]
        for operation in native["operations"]:
            name = operation["name"]
            if operation.get("tool", "unset") is None:
                # Intent-level alias: it maps to a tool at plan time, not a tool name.
                self.assertIn("alias_of", operation, name)
                continue
            self.assertIn(
                f'"name": "{name}"',
                source,
                f"{name} is declared in the manifest but absent from server.py",
            )

    def test_documented_write_operations_classify_as_writes(self) -> None:
        from connector_runtime import classify_operation

        for connector in load_manifest()["connectors"]:
            for operation in connector["operations"]:
                if operation["read_only"]:
                    continue
                self.assertEqual(
                    classify_operation(operation["name"]),
                    "write",
                    f"{operation['name']} is declared as a write but ranks as a read",
                )

    def test_tool_mapping_points_at_real_server_tools(self) -> None:
        source = SERVER_PATH.read_text(encoding="utf-8")
        checked = 0
        for connector in load_manifest()["connectors"]:
            mapping = connector.get("tool_mapping")
            if not mapping:
                continue
            for key, entry in mapping.items():
                if key.startswith("_"):
                    continue
                checked += 1
                self.assertIn(
                    f'"name": "{entry["tool"]}"',
                    source,
                    f"{connector['id']}.{key} maps to {entry['tool']}, "
                    "which server.py does not define",
                )
        self.assertGreaterEqual(checked, 8, "too few tool mappings were checked")

    def test_manifest_round_trips_through_json(self) -> None:
        manifest = load_manifest()
        text = json.dumps(manifest, ensure_ascii=True, sort_keys=True)
        self.assertEqual(json.loads(text), manifest)
        self.assertTrue(text.isascii())

    def test_manifest_passes_the_offline_contract_matrix(self) -> None:
        """The published descriptors must survive the existing e2e gate."""

        from connector_e2e import run_contract_matrix

        report = run_contract_matrix(load_manifest()["connectors"])
        self.assertEqual(report["network_calls"], 0)
        self.assertEqual(report["totals"]["failed"], 0, report["results"])
        # Every published descriptor uses the placeholder host, so the matrix
        # cannot resolve an endpoint and must skip rather than claim a pass.
        # A deployment that substitutes a real host is checked separately.
        statuses = {
            item["connector_id"]: item["status"] for item in report["results"]
        }
        for connector_id in REQUIRED_IDS:
            self.assertEqual(statuses[connector_id], "skip", connector_id)
        native_checks = next(
            item["checks"]
            for item in report["results"]
            if item["connector_id"] == "x-native"
        )
        skipped = [item["check"] for item in native_checks if item["status"] == "skip"]
        self.assertEqual(skipped, ["endpoint_resolvable"])

    def test_deployed_native_endpoint_passes_the_matrix(self) -> None:
        """With a real local endpoint, the native descriptor passes offline."""

        from connector_e2e import run_contract_matrix

        connectors = load_manifest()["connectors"]
        deployed = []
        for connector in connectors:
            item = dict(connector)
            if item["id"] == "x-native":
                item["endpoint"] = "http://127.0.0.1:8765/mcp"
            deployed.append(item)
        report = run_contract_matrix(deployed)
        native = next(
            item for item in report["results"] if item["connector_id"] == "x-native"
        )
        self.assertEqual(native["status"], "pass", native["checks"])
        self.assertEqual(report["network_calls"], 0)


if __name__ == "__main__":
    unittest.main()
