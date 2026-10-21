"""Offline contract and end-to-end acceptance matrix for MCP connectors.

The matrix answers one narrow question: does a connector descriptor satisfy
the published contract well enough to attempt a connection? It never opens a
socket and always reports ``network_calls: 0``. The default probe inspects the
descriptor schema, endpoint shape, auth mode, status, operations, and
``read_only_default`` only.

Passing this matrix is not proof that a connector works. Real OAuth consent,
remote multi-tenant deployments, and external official MCP connections must be
verified separately with a caller-supplied probe and live evidence. The local
``x-native`` mock may pass on the offline contract alone because it has no
remote dependency.

Run the tests from the x-starter directory:

    python -X utf8 -m unittest tests.test_connector_e2e -v
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

__all__ = [
    "E2EResult",
    "REPORT_SCHEMA",
    "NATIVE_CONNECTOR_ID",
    "default_probe",
    "dumps_report",
    "run_contract_matrix",
]

REPORT_SCHEMA = "x-starter-connector-e2e/v1"
NATIVE_CONNECTOR_ID = "x-native"
NETWORK_CALLS = 0

CONNECTOR_STATUSES: tuple[str, ...] = ("pass", "fail", "skip")
DESCRIPTOR_STATUSES: tuple[str, ...] = ("available", "degraded", "disabled", "optional")
AUTH_MODES: tuple[str, ...] = ("none", "oauth", "host", "user")
KINDS: tuple[str, ...] = ("mcp", "api", "browser", "web_intent")
CHECK_STATUSES: tuple[str, ...] = ("pass", "fail", "skip")

_REQUIRED_FIELDS: tuple[str, ...] = (
    "connector_id",
    "title",
    "version",
    "kind",
    "endpoint",
    "auth",
    "status",
    "operations",
    "read_only_default",
)
_SECRET_KEY_PARTS: tuple[str, ...] = (
    "token",
    "secret",
    "password",
    "authorization",
    "cookie",
)
_LOCAL_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})
_MAX_KEY_DEPTH = 8

Probe = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class E2EResult:
    """Outcome for one connector descriptor."""

    connector_id: str
    checks: list[dict[str, Any]]
    passed: int
    failed: int
    skipped: int
    status: str  # "pass" | "fail" | "skip"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe copy that the caller may mutate."""

        return {
            "connector_id": self.connector_id,
            "checks": [dict(item) for item in self.checks],
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "status": self.status,
        }


def _check(name: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {"check": name, "status": status, "detail": detail}
    for key in sorted(extra):
        record[key] = extra[key]
    return record


def _result(connector_id: str, checks: list[dict[str, Any]]) -> E2EResult:
    passed = sum(1 for item in checks if item.get("status") == "pass")
    failed = sum(1 for item in checks if item.get("status") == "fail")
    skipped = sum(1 for item in checks if item.get("status") == "skip")
    if failed:
        status = "fail"
    elif skipped:
        status = "skip"
    else:
        status = "pass"
    return E2EResult(
        connector_id=connector_id,
        checks=checks,
        passed=passed,
        failed=failed,
        skipped=skipped,
        status=status,
    )


def _descriptor_label(descriptor: Any, index: int) -> str:
    if isinstance(descriptor, dict):
        value = descriptor.get("connector_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"<descriptor[{index}]>"


def _looks_like_placeholder(endpoint: str) -> bool:
    lowered = endpoint.lower()
    return (
        "your_" in lowered
        or ".invalid" in lowered
        or "example.invalid" in lowered
    )


def _scan_secret_keys(value: Any, *, depth: int = 0, seen: set[int] | None = None) -> list[str]:
    """Return dotted key paths whose names look like credentials."""

    if depth > _MAX_KEY_DEPTH:
        return []
    if seen is None:
        seen = set()
    if id(value) in seen:
        return []
    if isinstance(value, dict):
        seen.add(id(value))
        found: list[str] = []
        for key, child in value.items():
            if isinstance(key, str):
                lowered = key.lower()
                if any(part in lowered for part in _SECRET_KEY_PARTS):
                    found.append(key)
            found.extend(
                f"{key}.{item}" if isinstance(key, str) else item
                for item in _scan_secret_keys(child, depth=depth + 1, seen=seen)
            )
        return found
    if isinstance(value, (list, tuple)):
        seen.add(id(value))
        found = []
        for index, child in enumerate(value):
            found.extend(
                f"[{index}].{item}"
                for item in _scan_secret_keys(child, depth=depth + 1, seen=seen)
            )
        return found
    return []


def _endpoint_checks(endpoint: Any, kind: Any) -> list[dict[str, Any]]:
    if endpoint is None:
        detail = "No endpoint; the connector is local or intent based."
        return [_check("endpoint_format", "pass", detail)]
    if not isinstance(endpoint, str):
        return [_check("endpoint_format", "fail", "Endpoint must be a string or null.")]
    if not endpoint or endpoint != endpoint.strip() or any(ch.isspace() for ch in endpoint):
        return [_check("endpoint_format", "fail", "Endpoint must be a non-empty URL without whitespace.")]

    parts = urlsplit(endpoint)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return [_check("endpoint_format", "fail", "Endpoint must be an absolute http(s) URL with a host.")]
    if parts.username or parts.password:
        return [_check("endpoint_format", "fail", "Endpoint must not embed user information.")]
    if parts.fragment:
        return [_check("endpoint_format", "fail", "Endpoint must not embed a fragment.")]
    if parts.scheme == "http" and parts.hostname not in _LOCAL_HOSTS:
        return [_check("endpoint_format", "fail", "Plain http is allowed only for localhost endpoints.")]
    if kind == "web_intent" and parts.scheme != "https":
        return [_check("endpoint_format", "fail", "Web intent endpoints must use https.")]

    checks = [_check("endpoint_format", "pass", "Endpoint is a well-formed URL.")]
    if _looks_like_placeholder(endpoint):
        checks.append(
            _check(
                "endpoint_resolvable",
                "skip",
                "Endpoint is a documented placeholder; the matrix does not resolve it.",
            )
        )
    return checks


def _operation_checks(operations: Any) -> list[dict[str, Any]]:
    if not isinstance(operations, list) or not operations:
        return [_check("operations_shape", "fail", "Operations must be a non-empty list.")]

    names: list[str] = []
    malformed: list[str] = []
    unsafe: list[str] = []
    for index, operation in enumerate(operations):
        label = f"[{index}]"
        if isinstance(operation, str):
            name = operation.strip()
            if not name:
                malformed.append(label)
            else:
                names.append(name)
            continue
        if not isinstance(operation, dict):
            malformed.append(label)
            continue
        name_value = operation.get("name")
        if not isinstance(name_value, str) or not name_value.strip():
            malformed.append(label)
            continue
        name = name_value.strip()
        names.append(name)
        read_only = operation.get("read_only")
        confirmation = operation.get("requires_confirmation")
        if read_only is not None and not isinstance(read_only, bool):
            malformed.append(label)
        if confirmation is not None and not isinstance(confirmation, bool):
            malformed.append(label)
        if read_only is False and confirmation is not True:
            unsafe.append(name)

    checks: list[dict[str, Any]] = []
    if malformed:
        checks.append(
            _check(
                "operations_shape",
                "fail",
                "Each operation needs a name and boolean safety flags.",
                invalid=malformed,
            )
        )
    else:
        checks.append(
            _check(
                "operations_shape",
                "pass",
                "Every operation declares a usable name.",
                count=len(operations),
            )
        )

    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        checks.append(
            _check(
                "operation_names_unique",
                "fail",
                "Operation names must be unique.",
                duplicates=duplicates,
            )
        )
    else:
        checks.append(_check("operation_names_unique", "pass", "Operation names are unique."))

    if unsafe:
        checks.append(
            _check(
                "operation_write_confirmation",
                "fail",
                "Write operations must set requires_confirmation to true.",
                operations=unsafe,
            )
        )
    else:
        checks.append(
            _check(
                "operation_write_confirmation",
                "pass",
                "No write operation runs without confirmation.",
            )
        )
    return checks


def default_probe(descriptor: dict[str, Any]) -> dict[str, Any]:
    """Validate a descriptor offline; never opens a socket.

    The return value is ``{"ok": bool, "checks": [...]}``. Checks cover the
    schema, endpoint shape, auth mode, status value, operations, and
    ``read_only_default``. A false ``ok`` means the descriptor is invalid, not
    that a remote service is down.
    """

    if not isinstance(descriptor, dict):
        return {
            "ok": False,
            "checks": [
                _check("descriptor_schema", "fail", "Descriptor must be a JSON object.")
            ],
        }

    checks: list[dict[str, Any]] = [
        _check("descriptor_schema", "pass", "Descriptor is a JSON object.")
    ]

    missing = [name for name in _REQUIRED_FIELDS if name not in descriptor]
    if missing:
        checks.append(
            _check(
                "required_fields",
                "fail",
                "Descriptor is missing required fields.",
                missing=missing,
            )
        )
        return {"ok": False, "checks": checks}
    checks.append(_check("required_fields", "pass", "All required fields are present."))

    connector_id = descriptor.get("connector_id")
    if (
        not isinstance(connector_id, str)
        or not connector_id
        or not all(
            ch.isalnum() or ch in "._-" for ch in connector_id
        )
        or not connector_id[0].isalnum()
    ):
        checks.append(
            _check(
                "connector_id_format",
                "fail",
                "connector_id must start with a letter or digit and use only [A-Za-z0-9._-].",
            )
        )
    else:
        checks.append(_check("connector_id_format", "pass", "connector_id format is valid."))

    title = descriptor.get("title")
    version = descriptor.get("version")
    if not isinstance(title, str) or not title.strip():
        checks.append(_check("title_format", "fail", "title must be a non-empty string."))
    else:
        checks.append(_check("title_format", "pass", "title is present."))
    if not isinstance(version, str) or not version.strip():
        checks.append(_check("version_format", "fail", "version must be a non-empty string."))
    else:
        checks.append(_check("version_format", "pass", "version is present."))

    kind = descriptor.get("kind")
    if kind not in KINDS:
        checks.append(
            _check(
                "kind_value",
                "fail",
                "kind must be one of mcp, api, browser, web_intent.",
            )
        )
    else:
        checks.append(_check("kind_value", "pass", f"kind is {kind}."))

    checks.extend(_endpoint_checks(descriptor.get("endpoint"), kind))

    auth = descriptor.get("auth")
    if auth not in AUTH_MODES:
        checks.append(
            _check(
                "auth_value",
                "fail",
                "auth must be one of none, oauth, host, user.",
            )
        )
    else:
        checks.append(_check("auth_value", "pass", f"auth is {auth}."))
        if auth == "oauth" and descriptor.get("endpoint") is None:
            checks.append(
                _check(
                    "auth_endpoint",
                    "fail",
                    "OAuth connectors need an endpoint to authorize against.",
                )
            )
        else:
            checks.append(_check("auth_endpoint", "pass", "Auth mode matches the endpoint."))

    status = descriptor.get("status")
    if status not in DESCRIPTOR_STATUSES:
        checks.append(
            _check(
                "status_value",
                "fail",
                "status must be one of available, degraded, disabled, optional.",
            )
        )
    else:
        checks.append(_check("status_value", "pass", f"status is {status}."))

    checks.extend(_operation_checks(descriptor.get("operations")))

    read_only_default = descriptor.get("read_only_default")
    if not isinstance(read_only_default, bool):
        checks.append(
            _check("read_only_default_type", "fail", "read_only_default must be a boolean.")
        )
    else:
        checks.append(
            _check(
                "read_only_default_type",
                "pass",
                f"read_only_default is {str(read_only_default).lower()}.",
            )
        )

    secret_keys = sorted(set(_scan_secret_keys(descriptor)))
    if secret_keys:
        checks.append(
            _check(
                "no_secret_keys",
                "fail",
                "Descriptors must not carry credential-shaped keys.",
                keys=secret_keys,
            )
        )
    else:
        checks.append(
            _check("no_secret_keys", "pass", "No credential-shaped keys are present.")
        )

    ok = not any(item["status"] == "fail" for item in checks)
    return {"ok": ok, "checks": checks}


def _probe_checks(probe: Probe, descriptor: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        payload = copy.deepcopy(descriptor)
    except Exception:
        return [
            _check(
                "probe_contract",
                "fail",
                "Descriptor cannot be copied for an isolated probe call.",
            )
        ]

    try:
        raw = probe(payload)
    except Exception as exc:
        # Keep the exception type only; messages may contain URLs or tokens.
        return [
            _check(
                "probe_exception",
                "fail",
                f"Probe raised {type(exc).__name__}.",
            )
        ]

    if not isinstance(raw, dict):
        return [_check("probe_contract", "fail", "Probe must return a JSON object.")]

    raw_checks = raw.get("checks", [])
    if raw_checks is None:
        raw_checks = []
    if not isinstance(raw_checks, list):
        return [
            _check("probe_contract", "fail", "Probe checks must be a list when present.")
        ]

    checks: list[dict[str, Any]] = []
    for index, item in enumerate(raw_checks):
        if not isinstance(item, dict):
            return [
                _check(
                    "probe_contract",
                    "fail",
                    "Every probe check must be an object.",
                    index=index,
                )
            ]
        name = item.get("check")
        status = item.get("status")
        detail = item.get("detail", "")
        if not isinstance(name, str) or not name:
            return [
                _check(
                    "probe_contract",
                    "fail",
                    "Every probe check needs a non-empty check name.",
                    index=index,
                )
            ]
        if status not in CHECK_STATUSES:
            return [
                _check(
                    "probe_contract",
                    "fail",
                    "Probe check status must be pass, fail, or skip.",
                    index=index,
                )
            ]
        if not isinstance(detail, str):
            return [
                _check(
                    "probe_contract",
                    "fail",
                    "Probe check detail must be a string.",
                    index=index,
                )
            ]
        record = {"check": name, "status": status, "detail": detail}
        for key in sorted(item):
            if key not in {"check", "status", "detail"}:
                record[key] = item[key]
        checks.append(record)

    declared_status = raw.get("status")
    declared_ok = raw.get("ok")
    has_fail = any(item["status"] == "fail" for item in checks)

    if declared_status is not None and declared_status not in CHECK_STATUSES:
        return [
            _check("probe_contract", "fail", "Probe status must be pass, fail, or skip.")
        ]
    if not isinstance(declared_ok, (bool, type(None))):
        return [_check("probe_contract", "fail", "Probe ok must be a boolean when present.")]

    if declared_ok is False or declared_status == "fail":
        if not has_fail:
            checks.append(
                _check("probe_result", "fail", "Probe reported failure without a failing check.")
            )
    elif declared_status == "skip":
        if not any(item["status"] == "skip" for item in checks):
            checks.append(
                _check("probe_result", "skip", "Probe reported a skip without a skipped check.")
            )
    elif not checks:
        checks.append(
            _check("probe_result", "pass", "Probe completed without reporting a failure.")
        )
    return checks


def _evaluate(index: int, descriptor: Any, probe: Probe | None) -> E2EResult:
    connector_id = _descriptor_label(descriptor, index)
    if not isinstance(descriptor, dict):
        return _result(
            connector_id,
            [_check("descriptor_schema", "fail", "Descriptor must be a JSON object.")],
        )

    offline = default_probe(descriptor)
    checks = [dict(item) for item in offline["checks"]]
    if any(item["status"] == "fail" for item in checks):
        return _result(connector_id, checks)

    if descriptor.get("status") == "disabled":
        checks.append(
            _check(
                "connector_runnable",
                "skip",
                "Connector is disabled; no run was attempted.",
            )
        )
        return _result(connector_id, checks)

    if probe is None:
        if connector_id == NATIVE_CONNECTOR_ID:
            checks.append(
                _check(
                    "connection_probe",
                    "pass",
                    "Local mock connector passed the offline contract.",
                )
            )
        else:
            checks.append(
                _check(
                    "connection_probe",
                    "skip",
                    "External connector needs an explicit probe; the offline contract cannot prove a live connection.",
                )
            )
        return _result(connector_id, checks)

    checks.extend(_probe_checks(probe, descriptor))
    return _result(connector_id, checks)


def _acceptance() -> dict[str, Any]:
    return {
        "meaning": (
            "A pass means the descriptor satisfies the offline contract. "
            "It is not a live connection, OAuth grant, or tenant check."
        ),
        "criteria": [
            "Any failed check makes the connector fail.",
            "With no failed check, any skipped check makes the connector skip.",
            "A connector passes only when every check passes.",
            "External connectors without an explicit probe always skip.",
            "The matrix itself performs no network calls.",
        ],
        "not_covered": [
            "Real OAuth consent and token refresh.",
            "Remote multi-tenant routing, quotas, and isolation.",
            "External official MCP connection health or tool results.",
            "Live X API reads or writes.",
        ],
    }


def run_contract_matrix(
    descriptors: list[dict[str, Any]],
    *,
    probe: Probe | None = None,
) -> dict[str, Any]:
    """Evaluate descriptors offline and return a JSON-safe report.

    ``probe`` is optional and caller-owned. It receives a deep copy of one
    descriptor and must return ``{"ok": bool, "checks": [...], "status": ...}``.
    Exceptions and malformed returns become failed checks. Any network call made
    by ``probe`` is outside this matrix and is not counted in ``network_calls``;
    the matrix itself makes none.
    """

    if probe is not None and not callable(probe):
        raise TypeError("probe must be callable or None")
    if isinstance(descriptors, (str, bytes, bytearray)) or not isinstance(descriptors, list):
        raise TypeError("descriptors must be a list of connector descriptors")

    results = [_evaluate(index, item, probe) for index, item in enumerate(descriptors)]
    passed = sum(1 for item in results if item.status == "pass")
    failed = sum(1 for item in results if item.status == "fail")
    skipped = sum(1 for item in results if item.status == "skip")
    if failed:
        overall = "fail"
    elif skipped:
        overall = "skip"
    else:
        overall = "pass"

    return {
        "schema": REPORT_SCHEMA,
        "network_calls": NETWORK_CALLS,
        "results": [item.to_dict() for item in results],
        "totals": {
            "connectors": len(results),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "status": overall,
            "checks": {
                "passed": sum(item.passed for item in results),
                "failed": sum(item.failed for item in results),
                "skipped": sum(item.skipped for item in results),
            },
        },
        "acceptance": _acceptance(),
    }


def dumps_report(report: dict[str, Any]) -> str:
    """Serialize a report deterministically for evidence files."""

    return json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
