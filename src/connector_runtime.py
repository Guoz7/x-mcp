"""Read-first connector runtime for the X starter MCP server.

The runtime is a pure description layer. It never opens a socket, reads a
credential file, or launches a connector. Callers register descriptors for the
connectors they know about, ask :class:`ConnectorRegistry` to rank them, and
turn the ranking into an :class:`ExecutionPlan` that downstream code can act
on. X is only one possible connector; nothing here is X specific.

Only the Python standard library is used.
"""

from __future__ import annotations

import copy
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "ConnectorCapability",
    "ConnectorDescriptor",
    "ConnectorError",
    "ConnectorRegistry",
    "ExecutionPlan",
    "KNOWN_AUTH",
    "KNOWN_KINDS",
    "KNOWN_STATUS",
    "build_execution_plan",
    "classify_operation",
]

KNOWN_KINDS: tuple[str, ...] = ("mcp", "api", "browser", "web_intent")
KNOWN_AUTH: tuple[str, ...] = ("none", "oauth", "host", "user")
KNOWN_STATUS: tuple[str, ...] = ("available", "degraded", "disabled")

DRAFT_MODE = "draft_only"
DEFAULT_MODE_ORDER: tuple[str, ...] = ("mcp", "api", "web_intent", "browser")

# Verbs that describe reading remote state. The first verb token in an
# operation name decides how the operation is classified.
READ_VERBS: frozenset[str] = frozenset(
    {
        "check",
        "count",
        "describe",
        "download",
        "exists",
        "export",
        "fetch",
        "find",
        "get",
        "health",
        "info",
        "inspect",
        "list",
        "lookup",
        "metrics",
        "open",
        "preview",
        "query",
        "read",
        "render",
        "resolve",
        "search",
        "show",
        "status",
        "validate",
        "view",
        "whoami",
    }
)

# Verbs that change remote state. Anything without a known verb is treated as
# a write so the default plan asks for confirmation.
WRITE_VERBS: frozenset[str] = frozenset(
    {
        "accept",
        "add",
        "approve",
        "attach",
        "block",
        "bookmark",
        "create",
        "delete",
        "dm",
        "edit",
        "follow",
        "invite",
        "like",
        "mute",
        "patch",
        "pin",
        "post",
        "publish",
        "put",
        "quote",
        "reject",
        "remove",
        "reply",
        "repost",
        "revoke",
        "schedule",
        "send",
        "set",
        "subscribe",
        "unblock",
        "unbookmark",
        "unfollow",
        "unlike",
        "unmute",
        "unpin",
        "unrepost",
        "unsubscribe",
        "update",
        "upload",
        "write",
    }
)

_TRUE_TOKENS: frozenset[str] = frozenset(
    {"1", "true", "yes", "y", "on", "enabled", "supported", "available", "ok"}
)
_FALSE_TOKENS: frozenset[str] = frozenset(
    {
        "",
        "0",
        "false",
        "no",
        "n",
        "off",
        "disabled",
        "unsupported",
        "unavailable",
        "none",
        "null",
    }
)

_API_KEYS: tuple[str, ...] = ("api", "rest", "http", "server_api")
_COMPUTER_KEYS: tuple[str, ...] = (
    "computer_use",
    "computeruse",
    "computer",
    "cua",
    "desktop",
    "browser",
)
_WEB_KEYS: tuple[str, ...] = (
    "web_intent",
    "webintent",
    "web",
    "web_url",
    "browser_intent",
)
_APPS_KEYS: tuple[str, ...] = ("mcp_apps", "mcpapps", "mcp_ui", "mcp_app", "apps", "ui")
_MCP_CONNECTOR_KEYS: tuple[str, ...] = (
    "mcp_connectors",
    "mcp_client",
    "remote_mcp",
    "connector_runtime",
    "mcp",
)


class ConnectorError(Exception):
    """A connector error that is safe to expose through an MCP tool result."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.retryable = bool(retryable)
        self.details = copy.deepcopy(dict(details or {}))

    def to_dict(self) -> dict[str, Any]:
        """Return a detached, JSON-friendly description of the error."""

        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": copy.deepcopy(self.details),
        }


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConnectorError(
            "invalid_descriptor",
            f"{label} must be a non-empty string",
            details={"field": label},
        )
    return value.strip()


def _metadata(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConnectorError(
            "invalid_descriptor",
            f"{label} must be a mapping",
            details={"field": label},
        )
    return copy.deepcopy(dict(value))


@dataclass(frozen=True)
class ConnectorCapability:
    """One operation family that a connector can serve."""

    name: str
    kind: str
    operations: tuple[str, ...] = ()
    auth: str = "none"
    read_only: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.operations, str):
            operations: tuple[str, ...] = (self.operations,)
        else:
            operations = tuple(str(item) for item in (self.operations or ()))
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "metadata", _metadata(self.metadata, "capability.metadata"))

    def matches(self, operation: str) -> bool:
        """Return True when this capability serves ``operation``."""

        target = _normalize_operation(operation)
        if not target:
            return False
        if _same_or_namespaced(target, _normalize_operation(self.name)):
            return True
        return any(
            _same_or_namespaced(target, _normalize_operation(item))
            for item in self.operations
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a detached, JSON-friendly copy of this capability."""

        return {
            "name": self.name,
            "kind": self.kind,
            "operations": list(self.operations),
            "auth": self.auth,
            "read_only": self.read_only,
            "metadata": copy.deepcopy(self.metadata),
        }


@dataclass(frozen=True)
class ConnectorDescriptor:
    """Everything the runtime needs to rank a connector without contacting it."""

    connector_id: str
    title: str
    version: str
    endpoint: str | None
    capabilities: tuple[ConnectorCapability, ...]
    status: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.capabilities, ConnectorCapability):
            capabilities: tuple[ConnectorCapability, ...] = (self.capabilities,)
        else:
            capabilities = tuple(self.capabilities or ())
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "metadata", _metadata(self.metadata, "descriptor.metadata"))

    def supports(self, operation: str) -> bool:
        """Return True when at least one declared capability serves ``operation``."""

        return any(capability.matches(operation) for capability in self.capabilities)

    def capabilities_for(self, operation: str) -> tuple[ConnectorCapability, ...]:
        """Return the capabilities that serve ``operation``."""

        return tuple(
            capability
            for capability in self.capabilities
            if capability.matches(operation)
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a detached, JSON-friendly copy of this descriptor."""

        return {
            "connector_id": self.connector_id,
            "title": self.title,
            "version": self.version,
            "endpoint": self.endpoint,
            "capabilities": [capability.to_dict() for capability in self.capabilities],
            "status": self.status,
            "metadata": copy.deepcopy(self.metadata),
        }


def _normalize_operation(value: str) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _same_or_namespaced(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    return left.endswith("." + right) or right.endswith("." + left)


def _tokens(operation: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(operation))
    return [token for token in re.split(r"[^a-zA-Z0-9]+", spaced.lower()) if token]


def classify_operation(operation: str) -> str:
    """Classify an operation as ``"read"`` or ``"write"``.

    The first verb token in the name decides the class, so ``get_dm_history``
    is a read while ``dm.send`` is a write. Names without a known verb are
    treated as writes, because a confirmation prompt is the safe default.
    """

    for token in _tokens(operation):
        if token in READ_VERBS:
            return "read"
        if token in WRITE_VERBS:
            return "write"
    return "write"


class ConnectorRegistry:
    """Thread-safe, in-memory catalog of connector descriptors.

    The registry performs no I/O. Returned descriptors are detached deep
    copies, so callers can mutate the result without changing the catalog.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, ConnectorDescriptor] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, connector_id: object) -> bool:
        if not isinstance(connector_id, str):
            return False
        with self._lock:
            return connector_id in self._items

    def register(self, descriptor: ConnectorDescriptor) -> None:
        """Add a descriptor.

        Raises :class:`ConnectorError` with code ``connector_already_registered``
        when the id is taken. Registration never replaces an existing entry,
        so a stale descriptor cannot silently shadow a live one.
        """

        _validate_descriptor(descriptor)
        connector_id = descriptor.connector_id
        with self._lock:
            if connector_id in self._items:
                raise ConnectorError(
                    "connector_already_registered",
                    f"connector {connector_id!r} is already registered",
                    details={"connector_id": connector_id},
                )
            self._items[connector_id] = copy.deepcopy(descriptor)

    def unregister(self, connector_id: str) -> None:
        """Remove a descriptor. Raises ``connector_not_found`` when absent."""

        key = connector_id.strip() if isinstance(connector_id, str) else ""
        if not key:
            raise ConnectorError(
                "connector_not_found",
                "connector_id must be a non-empty string",
                details={"connector_id": connector_id},
            )
        with self._lock:
            if key not in self._items:
                raise ConnectorError(
                    "connector_not_found",
                    f"connector {key!r} is not registered",
                    details={"connector_id": key},
                )
            del self._items[key]

    def get(self, connector_id: str) -> ConnectorDescriptor | None:
        """Return a detached copy of one descriptor, or None."""

        if not isinstance(connector_id, str):
            return None
        with self._lock:
            descriptor = self._items.get(connector_id.strip())
            return None if descriptor is None else copy.deepcopy(descriptor)

    def list(self) -> list[ConnectorDescriptor]:
        """Return detached copies ordered by ``connector_id``."""

        with self._lock:
            descriptors = [copy.deepcopy(item) for item in self._items.values()]
        descriptors.sort(key=lambda item: item.connector_id)
        return descriptors

    def describe(self) -> list[dict[str, Any]]:
        """Return the catalog as JSON-friendly dictionaries."""

        return [descriptor.to_dict() for descriptor in self.list()]

    def select(
        self,
        *,
        operation: str,
        preferred_kind: str | None = None,
        require_auth: str | None = None,
    ) -> list[ConnectorDescriptor]:
        """Rank connectors that can serve ``operation``.

        Disabled connectors are never returned. Remaining connectors sort by
        status (``available`` before ``degraded``) and then by
        ``connector_id``. An empty or unknown operation returns an empty list
        instead of raising, so callers can always fall back to a draft.
        """

        target = _normalize_operation(operation)
        if preferred_kind is not None and preferred_kind not in KNOWN_KINDS:
            raise ConnectorError(
                "invalid_kind",
                f"preferred_kind must be one of {', '.join(KNOWN_KINDS)}",
                details={"preferred_kind": preferred_kind},
            )
        if require_auth is not None and require_auth not in KNOWN_AUTH:
            raise ConnectorError(
                "invalid_auth",
                f"require_auth must be one of {', '.join(KNOWN_AUTH)}",
                details={"require_auth": require_auth},
            )
        if not target:
            return []

        ranked: list[tuple[tuple[int, str], ConnectorDescriptor]] = []
        for descriptor in self.list():
            if descriptor.status == "disabled":
                continue
            capabilities = descriptor.capabilities_for(target)
            if preferred_kind is not None:
                capabilities = tuple(
                    capability
                    for capability in capabilities
                    if capability.kind == preferred_kind
                )
            if require_auth is not None:
                capabilities = tuple(
                    capability
                    for capability in capabilities
                    if capability.auth == require_auth
                )
            if not capabilities:
                continue
            status_rank = 0 if descriptor.status == "available" else 1
            ranked.append(((status_rank, descriptor.connector_id), descriptor))

        ranked.sort(key=lambda pair: pair[0])
        return [descriptor for _, descriptor in ranked]

    def negotiate(
        self, client_capabilities: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Normalize client capabilities into a stable structure.

        Missing, partial, or malformed input never raises. Defaults describe a
        plain MCP client: the server may call HTTP APIs and hand links to the
        user, but browser automation and MCP Apps need an explicit opt-in.
        """

        caps: Mapping[str, Any] = (
            client_capabilities if isinstance(client_capabilities, Mapping) else {}
        )
        api = _coerce_flag(_lookup(caps, _API_KEYS), True)
        computer_use = _coerce_flag(_lookup(caps, _COMPUTER_KEYS), False)
        web_intent = _coerce_flag(_lookup(caps, _WEB_KEYS), True)
        mcp_apps = _coerce_flag(_lookup(caps, _APPS_KEYS), False)
        mcp_connectors = _coerce_flag(_lookup(caps, _MCP_CONNECTOR_KEYS), True)

        execution_modes: list[str] = []
        if mcp_connectors:
            execution_modes.append("mcp")
        if api:
            execution_modes.append("api")
        if web_intent:
            execution_modes.append("web_intent")
        if computer_use:
            execution_modes.append("browser")
        execution_modes.append(DRAFT_MODE)

        return {
            "api": api,
            "computer_use": computer_use,
            "web_intent": web_intent,
            "mcp_apps": mcp_apps,
            "execution_modes": execution_modes,
        }


def _key_token(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _lookup(caps: Mapping[str, Any], aliases: tuple[str, ...]) -> Any:
    if not caps:
        return None
    normalized: dict[str, Any] = {}
    for key, value in caps.items():
        if isinstance(key, str):
            normalized.setdefault(_key_token(key), value)
    for alias in aliases:
        token = _key_token(alias)
        if token in normalized:
            return normalized[token]
    return None


def _coerce_flag(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
        return default
    if isinstance(value, Mapping):
        for key in ("enabled", "supported", "available", "capable", "value", "ok"):
            if key in value:
                return _coerce_flag(value[key], default)
        return True if value else default
    if isinstance(value, (list, tuple, set, frozenset)):
        return len(value) > 0
    return default


def _validate_descriptor(descriptor: ConnectorDescriptor) -> None:
    if not isinstance(descriptor, ConnectorDescriptor):
        raise ConnectorError(
            "invalid_descriptor",
            "descriptor must be a ConnectorDescriptor",
            details={"field": "descriptor"},
        )
    _require_text(descriptor.connector_id, "connector_id")
    _require_text(descriptor.title, "title")
    _require_text(descriptor.version, "version")
    if descriptor.endpoint is not None and not isinstance(descriptor.endpoint, str):
        raise ConnectorError(
            "invalid_descriptor",
            "endpoint must be a string or None",
            details={"field": "endpoint"},
        )
    if descriptor.status not in KNOWN_STATUS:
        raise ConnectorError(
            "invalid_descriptor",
            f"status must be one of {', '.join(KNOWN_STATUS)}",
            details={"field": "status", "value": descriptor.status},
        )
    if not isinstance(descriptor.capabilities, tuple):
        raise ConnectorError(
            "invalid_descriptor",
            "capabilities must be a tuple",
            details={"field": "capabilities"},
        )

    seen_names: set[str] = set()
    for capability in descriptor.capabilities:
        if not isinstance(capability, ConnectorCapability):
            raise ConnectorError(
                "invalid_descriptor",
                "every capability must be a ConnectorCapability",
                details={"field": "capabilities"},
            )
        name = _require_text(capability.name, "capability.name")
        if name in seen_names:
            raise ConnectorError(
                "invalid_descriptor",
                f"duplicate capability name {name!r}",
                details={"field": "capability.name", "value": name},
            )
        seen_names.add(name)
        if capability.kind not in KNOWN_KINDS:
            raise ConnectorError(
                "invalid_descriptor",
                f"capability.kind must be one of {', '.join(KNOWN_KINDS)}",
                details={"field": "capability.kind", "value": capability.kind},
            )
        if capability.auth not in KNOWN_AUTH:
            raise ConnectorError(
                "invalid_descriptor",
                f"capability.auth must be one of {', '.join(KNOWN_AUTH)}",
                details={"field": "capability.auth", "value": capability.auth},
            )
        if not isinstance(capability.read_only, bool):
            raise ConnectorError(
                "invalid_descriptor",
                "capability.read_only must be a bool",
                details={"field": "capability.read_only"},
            )
        if not capability.operations:
            raise ConnectorError(
                "invalid_descriptor",
                f"capability {name!r} must list at least one operation",
                details={"field": "capability.operations", "value": name},
            )
        for operation in capability.operations:
            if not isinstance(operation, str) or not operation.strip():
                raise ConnectorError(
                    "invalid_descriptor",
                    "capability operations must be non-empty strings",
                    details={"field": "capability.operations", "value": name},
                )


@dataclass(frozen=True)
class ExecutionPlan:
    """How the server intends to serve one operation."""

    operation: str
    connector_id: str
    execution_mode: str
    requires_confirmation: bool
    fallback_modes: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Return a detached, JSON-friendly copy of the plan."""

        return {
            "operation": self.operation,
            "connector_id": self.connector_id,
            "execution_mode": self.execution_mode,
            "requires_confirmation": self.requires_confirmation,
            "fallback_modes": list(self.fallback_modes),
            "reason": self.reason,
        }


def _mode_order(preferred_kind: str | None) -> tuple[str, ...]:
    order: list[str] = [preferred_kind] if preferred_kind else []
    for mode in DEFAULT_MODE_ORDER:
        if mode not in order:
            order.append(mode)
    return tuple(order)


def _resolve_prefer(
    prefer: str | None, registry: ConnectorRegistry
) -> tuple[str | None, str | None]:
    if not isinstance(prefer, str) or not prefer.strip():
        return None, None
    value = prefer.strip()
    if value.lower() in KNOWN_KINDS:
        return value.lower(), None
    connector = registry.get(value)
    if connector is not None:
        return None, connector.connector_id
    raise ConnectorError(
        "invalid_kind",
        f"prefer must be a known kind or a registered connector_id: {value!r}",
        details={"prefer": value, "known_kinds": list(KNOWN_KINDS)},
    )


def _best_capability(
    descriptor: ConnectorDescriptor, operation: str, mode: str
) -> ConnectorCapability | None:
    for capability in descriptor.capabilities:
        if capability.kind == mode and capability.matches(operation):
            return capability
    return None


def build_execution_plan(
    *,
    operation: str,
    registry: ConnectorRegistry,
    client_capabilities: Mapping[str, Any] | None = None,
    prefer: str | None = None,
) -> ExecutionPlan:
    """Choose a connector and mode for ``operation``.

    Priority is explicit ``prefer``, then client capabilities, then connector
    status. Disabled connectors are never chosen. When nothing matches, the
    plan uses ``execution_mode="draft_only"`` with an empty ``connector_id``
    instead of raising. ``requires_confirmation`` follows the operation class:
    writes and unknown verbs ask for confirmation, reads do not.
    """

    if not isinstance(registry, ConnectorRegistry):
        raise ConnectorError(
            "invalid_argument",
            "registry must be a ConnectorRegistry",
            details={"field": "registry"},
        )
    target = operation.strip() if isinstance(operation, str) else ""
    if not target:
        raise ConnectorError(
            "invalid_operation",
            "operation must be a non-empty string",
            details={"field": "operation"},
        )

    negotiated = registry.negotiate(client_capabilities)
    allowed_modes = tuple(
        mode for mode in negotiated["execution_modes"] if mode != DRAFT_MODE
    )
    operation_class = classify_operation(target)
    requires_confirmation = operation_class == "write"

    preferred_kind, preferred_connector = _resolve_prefer(prefer, registry)
    notes: list[str] = []
    order = _mode_order(preferred_kind)

    for mode in order:
        if mode not in allowed_modes:
            continue
        candidates = registry.select(operation=target, preferred_kind=mode)
        if preferred_connector is not None:
            candidates = [
                descriptor
                for descriptor in candidates
                if descriptor.connector_id == preferred_connector
            ]
        if not candidates:
            continue
        descriptor = candidates[0]
        capability = _best_capability(descriptor, target, mode)
        if capability is None:
            continue

        fallbacks: list[str] = [
            candidate for candidate in order if candidate in allowed_modes and candidate != mode
        ]
        if DRAFT_MODE not in fallbacks:
            fallbacks.append(DRAFT_MODE)

        reason_parts = [
            f"operation {target!r} classified as {operation_class}",
            f"selected connector {descriptor.connector_id!r} capability "
            f"{capability.name!r} via {mode!r} (status={descriptor.status})",
        ]
        if descriptor.status != "available":
            reason_parts.append("connector is degraded so results may be partial")
        if requires_confirmation:
            reason_parts.append("write operations require user confirmation")
        reason_parts.extend(notes)

        return ExecutionPlan(
            operation=target,
            connector_id=descriptor.connector_id,
            execution_mode=mode,
            requires_confirmation=requires_confirmation,
            fallback_modes=tuple(fallbacks),
            reason="; ".join(reason_parts),
        )

    reason_parts = [
        f"operation {target!r} classified as {operation_class}",
        "no enabled connector matched with the current client capabilities",
        f"allowed modes: {', '.join(allowed_modes) if allowed_modes else 'none'}",
        "falling back to draft_only",
    ]
    if preferred_connector is not None:
        reason_parts.append(
            f"preferred connector {preferred_connector!r} was disabled or unsupported"
        )
    reason_parts.extend(notes)

    return ExecutionPlan(
        operation=target,
        connector_id="",
        execution_mode=DRAFT_MODE,
        requires_confirmation=requires_confirmation,
        fallback_modes=(),
        reason="; ".join(reason_parts),
    )
