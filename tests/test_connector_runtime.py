"""Unit tests for the read-first connector runtime."""

from __future__ import annotations

import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from connector_runtime import (  # noqa: E402
    ConnectorCapability,
    ConnectorDescriptor,
    ConnectorError,
    ConnectorRegistry,
    ExecutionPlan,
    build_execution_plan,
    classify_operation,
)


def _capability(
    name: str,
    operations: tuple[str, ...],
    *,
    kind: str = "mcp",
    auth: str = "oauth",
    read_only: bool = True,
    metadata: dict | None = None,
) -> ConnectorCapability:
    return ConnectorCapability(
        name=name,
        kind=kind,
        operations=operations,
        auth=auth,
        read_only=read_only,
        metadata=metadata or {},
    )


def _descriptor(
    connector_id: str,
    *,
    status: str = "available",
    capabilities: tuple[ConnectorCapability, ...] | None = None,
    version: str = "1.0.0",
    endpoint: str | None = None,
    metadata: dict | None = None,
) -> ConnectorDescriptor:
    return ConnectorDescriptor(
        connector_id=connector_id,
        title=f"{connector_id} connector",
        version=version,
        endpoint=endpoint,
        capabilities=capabilities
        if capabilities is not None
        else (_capability("read_timeline", ("read_timeline", "get_posts")),),
        status=status,
        metadata=metadata or {},
    )


class RegistryBasicsTest(unittest.TestCase):
    def test_register_then_get_roundtrip(self) -> None:
        registry = ConnectorRegistry()
        registry.register(_descriptor("x-official", endpoint="https://example.invalid/mcp"))
        stored = registry.get("x-official")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.connector_id, "x-official")
        self.assertEqual(stored.status, "available")
        self.assertEqual(len(registry), 1)

    def test_duplicate_registration_is_rejected(self) -> None:
        registry = ConnectorRegistry()
        registry.register(_descriptor("dup"))
        with self.assertRaises(ConnectorError) as ctx:
            registry.register(_descriptor("dup", version="2.0.0"))
        self.assertEqual(ctx.exception.code, "connector_already_registered")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.details["connector_id"], "dup")
        kept = registry.get("dup")
        assert kept is not None
        self.assertEqual(kept.version, "1.0.0")

    def test_unregister_removes_and_missing_raises(self) -> None:
        registry = ConnectorRegistry()
        registry.register(_descriptor("temp"))
        registry.unregister("temp")
        self.assertIsNone(registry.get("temp"))
        with self.assertRaises(ConnectorError) as ctx:
            registry.unregister("temp")
        self.assertEqual(ctx.exception.code, "connector_not_found")

    def test_list_is_sorted_by_connector_id(self) -> None:
        registry = ConnectorRegistry()
        for name in ("zeta", "alpha", "mike", "bravo"):
            registry.register(_descriptor(name))
        self.assertEqual(
            [item.connector_id for item in registry.list()],
            ["alpha", "bravo", "mike", "zeta"],
        )

    def test_invalid_descriptor_is_rejected(self) -> None:
        registry = ConnectorRegistry()
        bad_status = _descriptor("bad-status", status="online")
        with self.assertRaises(ConnectorError) as ctx:
            registry.register(bad_status)
        self.assertEqual(ctx.exception.code, "invalid_descriptor")
        bad_kind = _descriptor(
            "bad-kind",
            capabilities=(_capability("read_timeline", ("read_timeline",), kind="grpc"),),
        )
        with self.assertRaises(ConnectorError):
            registry.register(bad_kind)
        self.assertEqual(len(registry), 0)


class SelectionTest(unittest.TestCase):
    def test_disabled_connector_is_never_selected(self) -> None:
        registry = ConnectorRegistry()
        registry.register(_descriptor("off", status="disabled"))
        self.assertEqual(registry.select(operation="read_timeline"), [])
        plan = build_execution_plan(
            operation="read_timeline", registry=registry, client_capabilities={}
        )
        self.assertEqual(plan.execution_mode, "draft_only")

    def test_available_ranks_before_degraded(self) -> None:
        registry = ConnectorRegistry()
        registry.register(_descriptor("aaa-degraded", status="degraded"))
        registry.register(_descriptor("zzz-available", status="available"))
        ordered = [item.connector_id for item in registry.select(operation="read_timeline")]
        self.assertEqual(ordered, ["zzz-available", "aaa-degraded"])

    def test_preferred_kind_filters_candidates(self) -> None:
        registry = ConnectorRegistry()
        registry.register(
            _descriptor(
                "api-one",
                capabilities=(_capability("read_timeline", ("read_timeline",), kind="api"),),
            )
        )
        registry.register(
            _descriptor(
                "mcp-one",
                capabilities=(_capability("read_timeline", ("read_timeline",), kind="mcp"),),
            )
        )
        mcp_only = registry.select(operation="read_timeline", preferred_kind="mcp")
        self.assertEqual([item.connector_id for item in mcp_only], ["mcp-one"])
        api_only = registry.select(operation="read_timeline", preferred_kind="api")
        self.assertEqual([item.connector_id for item in api_only], ["api-one"])

    def test_require_auth_filters_candidates(self) -> None:
        registry = ConnectorRegistry()
        registry.register(
            _descriptor(
                "user-auth",
                capabilities=(
                    _capability("read_timeline", ("read_timeline",), auth="user"),
                ),
            )
        )
        registry.register(
            _descriptor(
                "host-auth",
                capabilities=(
                    _capability("read_timeline", ("read_timeline",), auth="host"),
                ),
            )
        )
        self.assertEqual(
            [item.connector_id for item in registry.select(operation="read_timeline", require_auth="host")],
            ["host-auth"],
        )

    def test_unknown_operation_returns_empty_list(self) -> None:
        registry = ConnectorRegistry()
        registry.register(_descriptor("x-official"))
        self.assertEqual(registry.select(operation="publish_launch_poem"), [])
        self.assertEqual(registry.select(operation=""), [])

    def test_namespaced_operations_match_plain_operations(self) -> None:
        registry = ConnectorRegistry()
        registry.register(
            _descriptor(
                "x-official",
                capabilities=(
                    _capability("x.timeline.read", ("x.timeline.read",)),
                ),
            )
        )
        self.assertEqual(
            [item.connector_id for item in registry.select(operation="timeline.read")],
            ["x-official"],
        )


class NegotiateTest(unittest.TestCase):
    def test_empty_input_returns_stable_structure(self) -> None:
        registry = ConnectorRegistry()
        result = registry.negotiate({})
        self.assertEqual(
            set(result),
            {"api", "computer_use", "web_intent", "mcp_apps", "execution_modes"},
        )
        self.assertIs(result["api"], True)
        self.assertIs(result["computer_use"], False)
        self.assertIs(result["web_intent"], True)
        self.assertIs(result["mcp_apps"], False)
        self.assertIn("api", result["execution_modes"])
        self.assertIn("draft_only", result["execution_modes"])
        self.assertNotIn("browser", result["execution_modes"])

    def test_none_and_garbage_input_is_tolerated(self) -> None:
        registry = ConnectorRegistry()
        for payload in (None, [], "nope", 42):  # type: ignore[list-item]
            result = registry.negotiate(payload)  # type: ignore[arg-type]
            self.assertEqual(result, registry.negotiate({}))

    def test_flags_accept_bools_strings_and_nested_maps(self) -> None:
        registry = ConnectorRegistry()
        result = registry.negotiate(
            {
                "api": "false",
                "computer_use": {"supported": True},
                "web_intent": 0,
                "mcp_apps": ["io.modelcontextprotocol/ui"],
            }
        )
        self.assertFalse(result["api"])
        self.assertTrue(result["computer_use"])
        self.assertFalse(result["web_intent"])
        self.assertTrue(result["mcp_apps"])
        self.assertIn("browser", result["execution_modes"])
        self.assertNotIn("api", result["execution_modes"])


class ExecutionPlanTest(unittest.TestCase):
    def test_read_operation_does_not_require_confirmation(self) -> None:
        registry = ConnectorRegistry()
        registry.register(_descriptor("x-official"))
        plan = build_execution_plan(
            operation="read_timeline", registry=registry, client_capabilities={}
        )
        self.assertIsInstance(plan, ExecutionPlan)
        self.assertEqual(plan.connector_id, "x-official")
        self.assertEqual(plan.execution_mode, "mcp")
        self.assertFalse(plan.requires_confirmation)
        self.assertIn("draft_only", plan.fallback_modes)

    def test_write_operation_requires_confirmation(self) -> None:
        registry = ConnectorRegistry()
        registry.register(
            _descriptor(
                "x-official",
                capabilities=(
                    _capability("post", ("create_post",), read_only=False),
                ),
            )
        )
        plan = build_execution_plan(
            operation="create_post", registry=registry, client_capabilities={}
        )
        self.assertEqual(plan.connector_id, "x-official")
        self.assertTrue(plan.requires_confirmation)
        self.assertTrue(plan.fallback_modes[-1] == "draft_only")

    def test_unknown_verb_defaults_to_confirmation(self) -> None:
        self.assertEqual(classify_operation("frobnicate_timeline"), "write")
        self.assertEqual(classify_operation("read_timeline"), "read")
        self.assertEqual(classify_operation("get_dm_history"), "read")
        self.assertEqual(classify_operation("dm.send"), "write")

    def test_explicit_prefer_beats_client_capability(self) -> None:
        registry = ConnectorRegistry()
        registry.register(
            _descriptor(
                "api-one",
                capabilities=(_capability("read_timeline", ("read_timeline",), kind="api"),),
            )
        )
        registry.register(
            _descriptor(
                "browser-one",
                capabilities=(
                    _capability("read_timeline", ("read_timeline",), kind="browser", auth="host"),
                ),
            )
        )
        client = {"api": True, "computer_use": True}
        default_plan = build_execution_plan(
            operation="read_timeline", registry=registry, client_capabilities=client
        )
        self.assertEqual(default_plan.execution_mode, "api")
        preferred = build_execution_plan(
            operation="read_timeline",
            registry=registry,
            client_capabilities=client,
            prefer="browser",
        )
        self.assertEqual(preferred.execution_mode, "browser")
        self.assertEqual(preferred.connector_id, "browser-one")

    def test_client_capability_beats_connector_status(self) -> None:
        registry = ConnectorRegistry()
        registry.register(
            _descriptor(
                "mcp-degraded",
                status="degraded",
                capabilities=(_capability("read_timeline", ("read_timeline",), kind="mcp"),),
            )
        )
        registry.register(
            _descriptor(
                "browser-available",
                capabilities=(
                    _capability("read_timeline", ("read_timeline",), kind="browser", auth="host"),
                ),
            )
        )
        api_first = build_execution_plan(
            operation="read_timeline", registry=registry, client_capabilities={}
        )
        self.assertEqual(api_first.execution_mode, "mcp")
        self.assertEqual(api_first.connector_id, "mcp-degraded")
        browser_allowed = build_execution_plan(
            operation="read_timeline",
            registry=registry,
            client_capabilities={"computer_use": True},
            prefer="browser",
        )
        self.assertEqual(browser_allowed.connector_id, "browser-available")

    def test_prefer_by_connector_id_pins_the_connector(self) -> None:
        registry = ConnectorRegistry()
        registry.register(_descriptor("alpha"))
        registry.register(_descriptor("beta"))
        plan = build_execution_plan(
            operation="read_timeline",
            registry=registry,
            client_capabilities={},
            prefer="beta",
        )
        self.assertEqual(plan.connector_id, "beta")

    def test_draft_only_when_nothing_is_available(self) -> None:
        registry = ConnectorRegistry()
        plan = build_execution_plan(
            operation="read_timeline", registry=registry, client_capabilities={}
        )
        self.assertEqual(plan.execution_mode, "draft_only")
        self.assertEqual(plan.connector_id, "")
        self.assertEqual(plan.fallback_modes, ())
        self.assertFalse(plan.requires_confirmation)
        self.assertIn("draft_only", plan.reason)

    def test_draft_only_is_used_when_client_lacks_the_mode(self) -> None:
        registry = ConnectorRegistry()
        registry.register(
            _descriptor(
                "browser-only",
                capabilities=(
                    _capability("read_timeline", ("read_timeline",), kind="browser", auth="host"),
                ),
            )
        )
        plan = build_execution_plan(
            operation="read_timeline",
            registry=registry,
            client_capabilities={"computer_use": False},
        )
        self.assertEqual(plan.execution_mode, "draft_only")

    def test_invalid_arguments_raise_connector_error(self) -> None:
        registry = ConnectorRegistry()
        with self.assertRaises(ConnectorError) as ctx:
            build_execution_plan(operation="  ", registry=registry, client_capabilities={})
        self.assertEqual(ctx.exception.code, "invalid_operation")
        with self.assertRaises(ConnectorError) as ctx2:
            build_execution_plan(
                operation="read_timeline", registry=registry, client_capabilities={}, prefer="grpc"
            )
        self.assertEqual(ctx2.exception.code, "invalid_kind")


class CopySafetyTest(unittest.TestCase):
    def test_returned_descriptor_is_detached_from_registry(self) -> None:
        registry = ConnectorRegistry()
        registry.register(_descriptor("x-official", metadata={"tier": "free"}))
        first = registry.get("x-official")
        assert first is not None
        first.metadata["tier"] = "mutated"
        second = registry.get("x-official")
        assert second is not None
        self.assertEqual(second.metadata["tier"], "free")

    def test_mutating_source_descriptor_after_register_has_no_effect(self) -> None:
        registry = ConnectorRegistry()
        source = _descriptor("x-official", metadata={"tier": "free"})
        registry.register(source)
        source.metadata["tier"] = "mutated"
        stored = registry.get("x-official")
        assert stored is not None
        self.assertEqual(stored.metadata["tier"], "free")

    def test_serialized_snapshots_are_json_ready_and_detached(self) -> None:
        registry = ConnectorRegistry()
        registry.register(
            _descriptor(
                "x-official",
                endpoint="https://example.invalid/mcp",
                metadata={"labels": ["official"], "limits": {"rps": 3}},
            )
        )
        snapshot = registry.describe()
        encoded = json.dumps(snapshot)
        decoded = json.loads(encoded)
        self.assertEqual(decoded[0]["connector_id"], "x-official")
        self.assertEqual(decoded[0]["capabilities"][0]["operations"], ["read_timeline", "get_posts"])
        decoded[0]["metadata"]["labels"].append("mutated")
        fresh = registry.describe()
        self.assertEqual(fresh[0]["metadata"]["labels"], ["official"])
        self.assertNotIn("mutated", json.dumps(fresh))

    def test_plan_serializes_to_json(self) -> None:
        registry = ConnectorRegistry()
        registry.register(_descriptor("x-official"))
        plan = build_execution_plan(
            operation="read_timeline", registry=registry, client_capabilities={}
        )
        payload = plan.to_dict()
        roundtrip = json.loads(json.dumps(payload))
        self.assertEqual(roundtrip["execution_mode"], "mcp")
        self.assertIsInstance(roundtrip["fallback_modes"], list)


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_reads_and_writes_stay_consistent(self) -> None:
        registry = ConnectorRegistry()
        for index in range(8):
            registry.register(_descriptor(f"seed-{index:02d}"))
        errors: list[BaseException] = []
        barrier = threading.Barrier(9)

        def writer(worker: int) -> None:
            try:
                barrier.wait()
                for round_index in range(50):
                    connector_id = f"w{worker}-{round_index:03d}"
                    registry.register(_descriptor(connector_id))
                    fetched = registry.get(connector_id)
                    if fetched is None or fetched.connector_id != connector_id:
                        raise AssertionError("read-after-write mismatch")
                    registry.unregister(connector_id)
                    if registry.get(connector_id) is not None:
                        raise AssertionError("unregister did not remove entry")
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        def reader() -> None:
            try:
                barrier.wait()
                for _ in range(200):
                    listing = registry.list()
                    ids = [item.connector_id for item in listing]
                    if ids != sorted(ids):
                        raise AssertionError("list is not sorted")
                    registry.select(operation="read_timeline")
                    registry.negotiate({"api": True})
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(worker,)) for worker in range(8)]
        threads.append(threading.Thread(target=reader))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(
            [item.connector_id for item in registry.list()],
            [f"seed-{index:02d}" for index in range(8)],
        )


if __name__ == "__main__":
    unittest.main()
