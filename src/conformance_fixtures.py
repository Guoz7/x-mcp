"""Self-contained MCP server conformance fixtures for revision 2025-11-25.

The main server dispatcher can expose ``TOOL_DEFINITIONS``, ``RESOURCES``,
``RESOURCE_TEMPLATES``, and ``PROMPTS`` in its discovery responses.  It can
route fixture methods through ``handle`` and use ``needs_client_request`` and
``client_request_for`` to perform the server-to-client work required by the
sampling, elicitation, logging, and progress scenarios.

``client_request_for`` returns JSON-RPC request objects for sampling and
elicitation.  Logging and progress are notifications, so their objects contain
no ``id``.  The caller must send all returned objects before it sends the final
``tools/call`` result.

Callback results may be supplied to ``handle`` in any of these locations:

* ``params["clientResponses"]``
* ``params["_clientResponses"]``
* ``params["inputResponses"]``
* ``params["_meta"]["clientResponses"]``
* ``params["_meta"]["_clientResponses"]``
* ``params["_meta"]["inputResponses"]``

Each value may be a JSON-RPC response, a bare method result, a list of either,
or a mapping keyed by request id or method name.  If a dispatcher does not
provide callback results, the module uses deterministic fallback values.  This
keeps the fixture tool result well-formed while the client request itself still
travels over the real transport.

The module uses only the Python standard library and keeps all fixture state in
process memory.
"""

from __future__ import annotations

import itertools
import json
import re
import threading
from typing import Any
from urllib.parse import unquote


# The same minimal, valid assets used by the official reference fixture.
_TEST_IMAGE_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIA"
    "X8jx0gAAAABJRU5ErkJggg=="
)
_TEST_AUDIO_BASE64 = (
    "UklGRiYAAABXQVZFZm10IBAAAAABAAEAQB8AAAB9AAACABAAZGF0YQIAAAA="
)


JSON_SCHEMA_2020_12_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "$defs": {
        "address": {
            "$anchor": "addressDef",
            "type": "object",
            "properties": {
                "street": {"type": "string"},
                "city": {"type": "string"},
            },
        }
    },
    "properties": {
        "name": {"type": "string"},
        "address": {"$ref": "#/$defs/address"},
        "contactMethod": {"type": "string", "enum": ["phone", "email"]},
        "phone": {"type": "string"},
        "email": {"type": "string"},
    },
    "allOf": [{"anyOf": [{"required": ["phone"]}, {"required": ["email"]}]}],
    "if": {
        "properties": {"contactMethod": {"const": "phone"}},
        "required": ["contactMethod"],
    },
    "then": {"required": ["phone"]},
    "else": {"required": ["email"]},
    "additionalProperties": False,
}


def _empty_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def _content_output_schema() -> dict[str, Any]:
    """Declare the shape every fixture tool returns.

    The fixtures all return a content block list (optionally with ``isError``),
    so one honest schema covers them without inventing per-tool structure.
    """

    return {
        "type": "object",
        "required": ["content"],
        "properties": {
            "content": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["type"],
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["text", "image", "audio", "resource"],
                        },
                        "text": {"type": "string"},
                        "data": {"type": "string"},
                        "mimeType": {"type": "string"},
                        "resource": {"type": "object"},
                    },
                },
            },
            "isError": {"type": "boolean"},
        },
    }


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "test_simple_text",
        "title": "Simple text fixture",
        "description": "Tests simple text content response",
        "inputSchema": _empty_input_schema(),
    },
    {
        "name": "test_image_content",
        "title": "Image content fixture",
        "description": "Tests image content response",
        "inputSchema": _empty_input_schema(),
    },
    {
        "name": "test_audio_content",
        "title": "Audio content fixture",
        "description": "Tests audio content response",
        "inputSchema": _empty_input_schema(),
    },
    {
        "name": "test_embedded_resource",
        "title": "Embedded resource fixture",
        "description": "Tests embedded resource content response",
        "inputSchema": _empty_input_schema(),
    },
    {
        "name": "test_multiple_content_types",
        "title": "Mixed content fixture",
        "description": (
            "Tests response with multiple content types (text, image, resource)"
        ),
        "inputSchema": _empty_input_schema(),
    },
    {
        "name": "test_tool_with_logging",
        "title": "Logging fixture",
        "description": "Tests tool that emits log messages during execution",
        "inputSchema": _empty_input_schema(),
    },
    {
        "name": "test_error_handling",
        "title": "Error handling fixture",
        "description": "Tests error response handling",
        "inputSchema": _empty_input_schema(),
    },
    {
        "name": "test_tool_with_progress",
        "title": "Progress fixture",
        "description": "Tests tool that reports progress notifications",
        "inputSchema": _empty_input_schema(),
    },
    {
        "name": "test_sampling",
        "title": "Sampling fixture",
        "description": "Tests server-initiated sampling (LLM completion request)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The prompt to send to the LLM",
                }
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "test_elicitation",
        "title": "Elicitation fixture",
        "description": "Tests server-initiated elicitation (user input request)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The message to show the user",
                }
            },
            "required": ["message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "test_elicitation_sep1034_defaults",
        "title": "Elicitation defaults fixture",
        "description": "Tests elicitation with default values per SEP-1034",
        "inputSchema": _empty_input_schema(),
    },
    {
        "name": "test_elicitation_sep1330_enums",
        "title": "Elicitation enums fixture",
        "description": (
            "Tests elicitation with enum schema improvements per SEP-1330"
        ),
        "inputSchema": _empty_input_schema(),
    },
    {
        "name": "json_schema_2020_12_tool",
        "title": "JSON Schema 2020-12 fixture",
        "description": (
            "Tool with JSON Schema 2020-12 features for conformance testing "
            "(SEP-1613)"
        ),
        "inputSchema": JSON_SCHEMA_2020_12_INPUT_SCHEMA,
    },
]


for _fixture_tool in TOOL_DEFINITIONS:
    # Every fixture returns MCP content blocks, so one shared declaration is
    # accurate. setdefault keeps any future per-tool schema intact.
    _fixture_tool.setdefault("outputSchema", _content_output_schema())


RESOURCES: list[dict[str, Any]] = [
    {
        "uri": "test://static-text",
        "name": "static-text",
        "title": "Static Text Resource",
        "description": "A static text resource for testing",
        "mimeType": "text/plain",
    },
    {
        "uri": "test://static-binary",
        "name": "static-binary",
        "title": "Static Binary Resource",
        "description": "A static binary resource (image) for testing",
        "mimeType": "image/png",
    },
    {
        "uri": "test://watched-resource",
        "name": "watched-resource",
        "title": "Watched Resource",
        "description": "A resource that supports subscription testing",
        "mimeType": "text/plain",
    },
]


RESOURCE_TEMPLATES: list[dict[str, Any]] = [
    {
        "uriTemplate": "test://template/{id}/data",
        "name": "template",
        "title": "Resource Template",
        "description": "A resource template with parameter substitution",
        "mimeType": "application/json",
    }
]


PROMPTS: list[dict[str, Any]] = [
    {
        "name": "test_simple_prompt",
        "title": "Simple Test Prompt",
        "description": "A simple prompt without arguments",
        "arguments": [],
    },
    {
        "name": "test_prompt_with_arguments",
        "title": "Prompt With Arguments",
        "description": "A prompt with required arguments",
        "arguments": [
            {
                "name": "arg1",
                "description": "First test argument",
                "required": True,
            },
            {
                "name": "arg2",
                "description": "Second test argument",
                "required": True,
            },
        ],
    },
    {
        "name": "test_prompt_with_embedded_resource",
        "title": "Prompt With Embedded Resource",
        "description": "A prompt that includes an embedded resource",
        "arguments": [
            {
                "name": "resourceUri",
                "description": "URI of the resource to embed",
                "required": True,
            }
        ],
    },
    {
        "name": "test_prompt_with_image",
        "title": "Prompt With Image",
        "description": "A prompt that includes image content",
        "arguments": [],
    },
]


_CLIENT_TOOLS = frozenset(
    {
        "test_tool_with_logging",
        "test_tool_with_progress",
        "test_sampling",
        "test_elicitation",
        "test_elicitation_sep1034_defaults",
        "test_elicitation_sep1330_enums",
    }
)

_CLIENT_RESULT_KEYS = (
    "clientResponses",
    "_clientResponses",
    "inputResponses",
    "clientResults",
    "_clientResults",
)

_TEMPLATE_URI_RE = re.compile(r"^test://template/([^/?#]+)/data$")
_REQUEST_COUNTER = itertools.count(1)
_REQUEST_COUNTER_LOCK = threading.Lock()
_SUBSCRIPTIONS: set[str] = set()
_SUBSCRIPTIONS_LOCK = threading.Lock()


def needs_client_request(name: str) -> bool:
    """Return whether a fixture tool needs server-to-client activity."""

    return name in _CLIENT_TOOLS


def client_request_for(
    name: str,
    arguments: dict[str, Any],
    request_meta: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build the client messages required before a fixture tool completes.

    Sampling and elicitation entries are JSON-RPC requests and carry an ``id``.
    Logging and progress entries are JSON-RPC notifications and carry no ``id``.
    """

    meta = request_meta if isinstance(request_meta, dict) else {}
    messages: list[dict[str, Any]] = []

    if name == "test_tool_with_logging":
        for text in (
            "Tool execution started",
            "Tool processing data",
            "Tool execution completed",
        ):
            messages.append(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/message",
                    "params": {
                        "level": "info",
                        "logger": "conformance-fixture",
                        "data": text,
                    },
                }
            )
        return messages

    if name == "test_tool_with_progress":
        if "progressToken" not in meta:
            return messages
        progress_token = meta["progressToken"]
        for progress in (0, 50, 100):
            messages.append(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {
                        "progressToken": progress_token,
                        "progress": progress,
                        "total": 100,
                        "message": f"Completed step {progress} of 100",
                    },
                }
            )
        return messages

    if name == "test_sampling":
        prompt = arguments.get("prompt", "") if isinstance(arguments, dict) else ""
        messages.append(
            {
                "jsonrpc": "2.0",
                "id": _next_request_id("sampling"),
                "method": "sampling/createMessage",
                "params": {
                    "messages": [
                        {
                            "role": "user",
                            "content": {
                                "type": "text",
                                "text": str(prompt),
                            },
                        }
                    ],
                    "maxTokens": 100,
                },
            }
        )
        return messages

    if name == "test_elicitation":
        message = (
            arguments.get("message", "Please provide your information")
            if isinstance(arguments, dict)
            else "Please provide your information"
        )
        messages.append(
            {
                "jsonrpc": "2.0",
                "id": _next_request_id("elicitation"),
                "method": "elicitation/create",
                "params": {
                    "message": str(message),
                    "requestedSchema": {
                        "type": "object",
                        "properties": {
                            "username": {
                                "type": "string",
                                "description": "User's response",
                            },
                            "email": {
                                "type": "string",
                                "description": "User's email address",
                            },
                        },
                        "required": ["username", "email"],
                    },
                },
            }
        )
        return messages

    if name == "test_elicitation_sep1034_defaults":
        messages.append(
            {
                "jsonrpc": "2.0",
                "id": _next_request_id("elicitation-defaults"),
                "method": "elicitation/create",
                "params": {
                    "message": (
                        "Please review and update the form fields with defaults"
                    ),
                    "requestedSchema": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "User name",
                                "default": "John Doe",
                            },
                            "age": {
                                "type": "integer",
                                "description": "User age",
                                "default": 30,
                            },
                            "score": {
                                "type": "number",
                                "description": "User score",
                                "default": 95.5,
                            },
                            "status": {
                                "type": "string",
                                "description": "User status",
                                "enum": ["active", "inactive", "pending"],
                                "default": "active",
                            },
                            "verified": {
                                "type": "boolean",
                                "description": "Verification status",
                                "default": True,
                            },
                        },
                        "required": [],
                    },
                },
            }
        )
        return messages

    if name == "test_elicitation_sep1330_enums":
        messages.append(
            {
                "jsonrpc": "2.0",
                "id": _next_request_id("elicitation-enums"),
                "method": "elicitation/create",
                "params": {
                    "message": "Please select options from the enum fields",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {
                            "untitledSingle": {
                                "type": "string",
                                "description": "Select one option",
                                "enum": ["option1", "option2", "option3"],
                            },
                            "titledSingle": {
                                "type": "string",
                                "description": "Select one option with titles",
                                "oneOf": [
                                    {"const": "value1", "title": "First Option"},
                                    {"const": "value2", "title": "Second Option"},
                                    {"const": "value3", "title": "Third Option"},
                                ],
                            },
                            "legacyEnum": {
                                "type": "string",
                                "description": "Select one option (legacy)",
                                "enum": ["opt1", "opt2", "opt3"],
                                "enumNames": [
                                    "Option One",
                                    "Option Two",
                                    "Option Three",
                                ],
                            },
                            "untitledMulti": {
                                "type": "array",
                                "description": "Select multiple options",
                                "minItems": 1,
                                "maxItems": 3,
                                "items": {
                                    "type": "string",
                                    "enum": ["option1", "option2", "option3"],
                                },
                            },
                            "titledMulti": {
                                "type": "array",
                                "description": (
                                    "Select multiple options with titles"
                                ),
                                "minItems": 1,
                                "maxItems": 3,
                                "items": {
                                    "anyOf": [
                                        {
                                            "const": "value1",
                                            "title": "First Choice",
                                        },
                                        {
                                            "const": "value2",
                                            "title": "Second Choice",
                                        },
                                        {
                                            "const": "value3",
                                            "title": "Third Choice",
                                        },
                                    ]
                                },
                            },
                        },
                        "required": [],
                    },
                },
            }
        )
        return messages

    return messages


def handle(method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the result body for a fixture method, or ``None`` if not handled."""

    values = params if isinstance(params, dict) else {}

    if method == "logging/setLevel":
        return {}
    if method == "tools/list":
        return {"tools": TOOL_DEFINITIONS}
    if method == "tools/call":
        return _handle_tool_call(values)
    if method == "resources/list":
        return {"resources": RESOURCES}
    if method == "resources/templates/list":
        return {"resourceTemplates": RESOURCE_TEMPLATES}
    if method == "resources/read":
        return _read_resource(values)
    if method == "resources/subscribe":
        uri = _required_string(values, "uri")
        with _SUBSCRIPTIONS_LOCK:
            _SUBSCRIPTIONS.add(uri)
        return {}
    if method == "resources/unsubscribe":
        uri = _required_string(values, "uri")
        with _SUBSCRIPTIONS_LOCK:
            _SUBSCRIPTIONS.discard(uri)
        return {}
    if method == "prompts/list":
        return {"prompts": PROMPTS}
    if method == "prompts/get":
        return _get_prompt(values)
    if method == "completion/complete":
        return _complete(values)
    return None


def _handle_tool_call(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str):
        raise ValueError("tools/call requires a string name")
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    # Reject malformed input before any fixture handler runs. The official
    # robustness checks send wrong types and expect -32602 rather than a
    # partially executed tool.
    _validate_arguments(name, arguments)
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        meta = {}

    if name == "test_simple_text":
        return _content(
            {"type": "text", "text": "This is a simple text response for testing."}
        )
    if name == "test_image_content":
        return _content(
            {
                "type": "image",
                "data": _TEST_IMAGE_BASE64,
                "mimeType": "image/png",
            }
        )
    if name == "test_audio_content":
        return _content(
            {
                "type": "audio",
                "data": _TEST_AUDIO_BASE64,
                "mimeType": "audio/wav",
            }
        )
    if name == "test_embedded_resource":
        return _content(
            {
                "type": "resource",
                "resource": {
                    "uri": "test://embedded-resource",
                    "mimeType": "text/plain",
                    "text": "This is an embedded resource content.",
                },
            }
        )
    if name == "test_multiple_content_types":
        return {
            "content": [
                {"type": "text", "text": "Multiple content types test:"},
                {
                    "type": "image",
                    "data": _TEST_IMAGE_BASE64,
                    "mimeType": "image/png",
                },
                {
                    "type": "resource",
                    "resource": {
                        "uri": "test://mixed-content-resource",
                        "mimeType": "application/json",
                        "text": _json_compact({"test": "data", "value": 123}),
                    },
                },
            ]
        }
    if name == "test_tool_with_logging":
        return _content(
            {
                "type": "text",
                "text": "Tool with logging executed successfully",
            }
        )
    if name == "test_error_handling":
        return {
            "content": [
                {
                    "type": "text",
                    "text": "This tool intentionally returns an error for testing",
                }
            ],
            "isError": True,
        }
    if name == "test_tool_with_progress":
        progress_token = meta.get("progressToken", 0)
        return _content({"type": "text", "text": str(progress_token)})
    if name == "test_sampling":
        result = _find_client_result(params, "sampling/createMessage", "sampling")
        model_response = _sampling_text(
            result,
            fallback="This is a test response from the client",
        )
        return _content({"type": "text", "text": f"LLM response: {model_response}"})
    if name == "test_elicitation":
        result = _find_client_result(params, "elicitation/create", "elicitation")
        action, content = _elicitation_parts(
            result,
            fallback_action="accept",
            fallback_content={
                "username": "testuser",
                "email": "test@example.com",
            },
        )
        return _content(
            {
                "type": "text",
                "text": (
                    f"User response: action={action}, content="
                    f"{_json_compact(content)}"
                ),
            }
        )
    if name == "test_elicitation_sep1034_defaults":
        result = _find_client_result(params, "elicitation/create", "elicitation")
        action, content = _elicitation_parts(
            result,
            fallback_action="accept",
            fallback_content={
                "name": "Jane Smith",
                "age": 25,
                "score": 88.0,
                "status": "inactive",
                "verified": False,
            },
        )
        return _content(
            {
                "type": "text",
                "text": (
                    f"Elicitation completed: action={action}, content="
                    f"{_json_compact(content)}"
                ),
            }
        )
    if name == "test_elicitation_sep1330_enums":
        result = _find_client_result(params, "elicitation/create", "elicitation")
        action, content = _elicitation_parts(
            result,
            fallback_action="accept",
            fallback_content={
                "untitledSingle": "option1",
                "titledSingle": "value1",
                "legacyEnum": "opt1",
                "untitledMulti": ["option1", "option2"],
                "titledMulti": ["value1", "value2"],
            },
        )
        return _content(
            {
                "type": "text",
                "text": (
                    f"Elicitation completed: action={action}, content="
                    f"{_json_compact(content)}"
                ),
            }
        )
    if name == "json_schema_2020_12_tool":
        return _content(
            {
                "type": "text",
                "text": (
                    "JSON Schema 2020-12 tool called with: "
                    + _json_compact(arguments)
                ),
            }
        )

    return {
        "content": [{"type": "text", "text": f"Unknown fixture tool: {name}"}],
        "isError": True,
    }


def _read_resource(params: dict[str, Any]) -> dict[str, Any]:
    uri = _required_string(params, "uri")
    if uri == "test://static-text":
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "text/plain",
                    "text": "This is the content of the static text resource.",
                }
            ]
        }
    if uri == "test://static-binary":
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "image/png",
                    "blob": _TEST_IMAGE_BASE64,
                }
            ]
        }
    if uri == "test://watched-resource":
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "text/plain",
                    "text": "Watched resource content",
                }
            ]
        }

    match = _TEMPLATE_URI_RE.fullmatch(uri)
    if match:
        resource_id = unquote(match.group(1))
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": _json_compact(
                        {
                            "id": resource_id,
                            "templateTest": True,
                            "data": f"Data for ID: {resource_id}",
                        }
                    ),
                }
            ]
        }

    raise ValueError(f"Resource not found: {uri}")


def _get_prompt(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str):
        raise ValueError("prompts/get requires a string name")
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    _validate_prompt_arguments(name, arguments)

    if name == "test_simple_prompt":
        return {
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": "This is a simple prompt for testing.",
                    },
                }
            ]
        }
    if name == "test_prompt_with_arguments":
        arg1 = str(arguments.get("arg1", ""))
        arg2 = str(arguments.get("arg2", ""))
        return {
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": (
                            f"Prompt with arguments: arg1='{arg1}', arg2='{arg2}'"
                        ),
                    },
                }
            ]
        }
    if name == "test_prompt_with_embedded_resource":
        resource_uri = str(arguments.get("resourceUri", "test://example-resource"))
        return {
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "resource",
                        "resource": {
                            "uri": resource_uri,
                            "mimeType": "text/plain",
                            "text": "Embedded resource content for testing.",
                        },
                    },
                },
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": "Please process the embedded resource above.",
                    },
                },
            ]
        }
    if name == "test_prompt_with_image":
        return {
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "image",
                        "data": _TEST_IMAGE_BASE64,
                        "mimeType": "image/png",
                    },
                },
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": "Please analyze the image above.",
                    },
                },
            ]
        }

    raise ValueError(f"Prompt not found: {name}")


def _complete(params: dict[str, Any]) -> dict[str, Any]:
    ref = params.get("ref")
    argument = params.get("argument")
    if (
        not isinstance(ref, dict)
        or ref.get("type") != "ref/prompt"
        or ref.get("name") != "test_prompt_with_arguments"
        or not isinstance(argument, dict)
    ):
        return {
            "completion": {
                "values": [],
                "total": 0,
                "hasMore": False,
            }
        }

    prefix = str(argument.get("value", ""))
    candidates = [
        "testValue1",
        "testValue2",
        "hello",
        "world",
        "argument-one",
        "argument-two",
    ]
    values = [
        candidate
        for candidate in candidates
        if candidate.casefold().startswith(prefix.casefold())
    ]
    if not values:
        values = candidates[:2]
    return {
        "completion": {
            "values": values,
            "total": len(values),
            "hasMore": False,
        }
    }


def _next_request_id(prefix: str) -> str:
    with _REQUEST_COUNTER_LOCK:
        number = next(_REQUEST_COUNTER)
    return f"conformance-{prefix}-{number}"


def _required_string(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return True


def _definition(name: str) -> dict[str, Any] | None:
    for definition in TOOL_DEFINITIONS:
        if definition.get("name") == name:
            return definition
    return None


def _validate_arguments(name: str, arguments: dict[str, Any]) -> None:
    """Check required keys and primitive types against the declared schema."""

    definition = _definition(name)
    if definition is None:
        raise ValueError(f"Unknown tool: {name}")
    schema = definition.get("inputSchema") or {}
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    for key in required:
        if key not in arguments:
            raise ValueError(f"Missing required argument: {key}")
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise ValueError(f"Unknown argument: {unknown[0]}")
    for key, value in arguments.items():
        rule = properties.get(key) or {}
        expected = rule.get("type")
        if isinstance(expected, str) and not _schema_type_matches(value, expected):
            raise ValueError(f"{key} must be of type {expected}")
        enum = rule.get("enum")
        if isinstance(enum, list) and value not in enum:
            raise ValueError(f"{key} must be one of {enum}")
    for clause in schema.get("allOf") or []:
        for branch in clause.get("anyOf") or []:
            if not any(key in arguments for key in branch.get("required") or []):
                raise ValueError(
                    "At least one of these arguments is required: "
                    + ", ".join(branch.get("required") or [])
                )


def _validate_prompt_arguments(name: str, arguments: dict[str, Any]) -> None:
    for prompt in PROMPTS:
        if prompt.get("name") != name:
            continue
        required = [
            item.get("name")
            for item in prompt.get("arguments") or []
            if item.get("required")
        ]
        for key in required:
            value = arguments.get(key)
            if not isinstance(value, str) or not value:
                raise ValueError(f"Missing required argument: {key}")
        return


def _content(item: dict[str, Any]) -> dict[str, Any]:
    return {"content": [item]}


def _json_compact(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _find_client_result(
    params: dict[str, Any],
    method: str,
    alias: str,
) -> dict[str, Any] | None:
    containers = [params]
    for key in ("_meta", "meta"):
        value = params.get(key)
        if isinstance(value, dict):
            containers.append(value)

    for container in containers:
        for key in _CLIENT_RESULT_KEYS:
            if key not in container:
                continue
            result = _select_result(container[key], method, alias)
            if result is not None:
                return result
    return None


def _select_result(
    value: Any,
    method: str,
    alias: str,
) -> dict[str, Any] | None:
    if isinstance(value, dict):
        for key in (method, alias):
            if key in value:
                unwrapped = _unwrap_result(value[key])
                if isinstance(unwrapped, dict):
                    return unwrapped
        expected_marker = "action" if method == "elicitation/create" else "content"
        for key, item in value.items():
            if key in _CLIENT_RESULT_KEYS:
                continue
            unwrapped = _unwrap_result(item)
            if (
                isinstance(unwrapped, dict)
                and expected_marker in unwrapped
            ):
                return unwrapped
        unwrapped = _unwrap_result(value)
        return unwrapped if isinstance(unwrapped, dict) else None
    if isinstance(value, list):
        for item in value:
            unwrapped = _unwrap_result(item)
            if isinstance(unwrapped, dict):
                return unwrapped
    return None


def _unwrap_result(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if "error" in value and ("id" in value or "jsonrpc" in value):
        return None
    if "result" in value and ("id" in value or "jsonrpc" in value):
        return value["result"]
    return value


def _sampling_text(result: dict[str, Any] | None, *, fallback: str) -> str:
    if not result:
        return fallback
    content = result.get("content")
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    message = result.get("message")
    if isinstance(message, dict):
        message_content = message.get("content")
        if (
            isinstance(message_content, dict)
            and isinstance(message_content.get("text"), str)
        ):
            return message_content["text"]
    return fallback


def _elicitation_parts(
    result: dict[str, Any] | None,
    *,
    fallback_action: str,
    fallback_content: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    if not result:
        return fallback_action, fallback_content
    action = result.get("action")
    if not isinstance(action, str):
        action = fallback_action
    content = result.get("content")
    if not isinstance(content, dict):
        content = fallback_content
    return action, content
