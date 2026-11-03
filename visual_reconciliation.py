"""Validate a read-only DSH visual diagnosis without promoting old actions.

The report is model-observed evidence, not proof that an earlier write changed
X. Raw tool results may be truncated; that limitation stays in the report.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

READ_ONLY_TOOLS = frozenset({
    "mcp_xstarter_browser_open", "mcp_xstarter_browser_observe",
    "mcp_xstarter_browser_screenshot", "mcp_xstarter_browser_close",
})
STATES = frozenset({"Follow", "Following", "Pending", "Unknown"})


def validate_visual_run(
    events: list[dict[str, Any]], *, expected_ref: str, attachment_bytes: bytes,
) -> dict[str, Any]:
    """Reject invented output contracts, absent images and write-tool calls."""

    errors = []
    calls = [row for row in events if row.get("type") == "tool_call"]
    call_ids = [row.get("callId") for row in calls]
    results = [row for row in events if row.get("type") == "tool_result"]
    result_ids = [row.get("callId") for row in results]
    if (
        any(not isinstance(value, str) or not value for value in call_ids + result_ids)
        or len(set(call_ids)) != len(call_ids)
        or len(set(result_ids)) != len(result_ids)
        or set(call_ids) != set(result_ids)
    ):
        errors.append("tool_call_result_identity_mismatch")
    tools = [row.get("tool") for row in calls]
    if any(tool not in READ_ONLY_TOOLS for tool in tools):
        errors.append("non_read_only_tool_called")
    expected_order = [
        "mcp_xstarter_browser_open", "mcp_xstarter_browser_observe",
        "mcp_xstarter_browser_screenshot", "mcp_xstarter_browser_close",
    ]
    if tools != expected_order:
        errors.append("unexpected_tool_sequence")
    result_by_id = {
        row.get("callId"): row for row in events if row.get("type") == "tool_result"
    }
    screenshot = None
    for call in calls:
        result = result_by_id.get(call.get("callId"))
        if not result or result.get("status") != "completed" or result.get("is_error") or result.get("isError"):
            errors.append("tool_result_not_completed")
            continue
        raw = result.get("result")
        if call.get("tool") == "mcp_xstarter_browser_screenshot":
            if (call.get("input") or {}).get("include_data") is not True:
                errors.append("image_data_not_requested")
            if isinstance(raw, str) and "image unavailable:" in raw:
                errors.append("image_admission_refused")
            try:
                # MCP text can have an attachment diagnostic after its JSON.
                value = json.JSONDecoder().raw_decode(raw.lstrip())[0] if isinstance(raw, str) else raw
                if value.get("ok") is False:
                    errors.append("screenshot_error_result")
                screenshot = value["data"]
                for key in ("observation_id", "fingerprint_sha256"):
                    if screenshot.get(key) != (call.get("input") or {}).get(key):
                        errors.append("screenshot_observation_mismatch")
            except (TypeError, ValueError, KeyError, AttributeError):
                errors.append("screenshot_metadata_unparsed")
    final_rows = [row for row in events if row.get("type") == "final"]
    if len(final_rows) != 1:
        errors.append("expected_one_final_result")
    observation = None
    try:
        observation = json.loads(final_rows[-1]["text"])
        if not isinstance(observation, dict):
            raise ValueError("not an object")
    except (IndexError, KeyError, TypeError, ValueError):
        observation = None
        errors.append("final_not_json_object")
    digest = hashlib.sha256(attachment_bytes).hexdigest()
    if not attachment_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        errors.append("attachment_not_png")
    if not isinstance(screenshot, dict) or screenshot.get("sha256") != digest:
        errors.append("screenshot_attachment_mismatch")
    if isinstance(observation, dict):
        if observation.get("target_ref") != expected_ref:
            errors.append("target_ref_mismatch")
        if observation.get("visible_state") not in STATES:
            errors.append("invalid_visible_state")
        if observation.get("pixels_available") is not True:
            errors.append("pixels_not_observed")
        if observation.get("screenshot_sha256") != digest:
            errors.append("model_image_mismatch")
        if observation.get("security_notice") is not None and not isinstance(observation["security_notice"], bool):
            errors.append("invalid_security_notice")
        if observation.get("security_notice") is True and observation.get("visible_state") != "Unknown":
            errors.append("security_interstitial_state_not_unknown")
        reason = observation.get("reason")
        if not isinstance(reason, str) or not reason or re.search(r"https?://|@[A-Za-z0-9_]", reason):
            errors.append("invalid_or_identifying_reason")
    return {
        "valid": not errors, "errors": errors,
        "target_ref": expected_ref,
        "visible_state": observation.get("visible_state") if observation else None,
        "screenshot_sha256": digest,
        "security_notice": observation.get("security_notice") if observation else None,
        "truncated_tool_results": sum(row.get("truncated") is True for row in result_by_id.values()),
        "evidence_level": "model_pixel_observation_not_historical_write_proof",
        "promote_historical_action": False,
        "retry_authorized": False,
    }


def validate_visual_files(log_path: str, image_path: str, expected_ref: str) -> dict[str, Any]:
    events = [json.loads(line) for line in Path(log_path).read_text("utf-8").splitlines() if line.startswith("{")]
    return validate_visual_run(events, expected_ref=expected_ref, attachment_bytes=Path(image_path).read_bytes())
