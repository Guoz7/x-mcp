"""Provider-neutral public Agent Help request board.

The local provider is the deterministic development implementation. The HTTP
provider uses the same contract for a future public service. The board stores
only public collaboration fields and never accepts browser credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


REQUEST_ID_RE = r"ahr_[A-Za-z0-9_-]{8,64}"
MAX_TITLE = 160
MAX_BODY = 4000
MAX_ITEMS = 20
MAX_TAG = 32
MAX_AGENT = 80
_SENSITIVE_KEYS = {"token", "secret", "password", "cookie", "authorization", "bearer"}


class BoardError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BoardError("INVALID_ARGUMENT", f"{field} must be a non-empty string.")
    value = value.strip()
    if len(value) > maximum:
        raise BoardError("INVALID_ARGUMENT", f"{field} must be {maximum} characters or fewer.")
    return value


def _list(value: Any, field: str, maximum: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise BoardError("INVALID_ARGUMENT", f"{field} must be a list with at most {maximum} items.")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > MAX_TAG:
            raise BoardError("INVALID_ARGUMENT", f"{field} contains an invalid item.")
        result.append(item.strip().lower())
    return list(dict.fromkeys(result))


def _public_request(arguments: dict[str, Any]) -> dict[str, Any]:
    for key in arguments:
        normalized = str(key).lower().replace("-", "_")
        if normalized in _SENSITIVE_KEYS or any(part in normalized for part in _SENSITIVE_KEYS):
            raise BoardError("SENSITIVE_PUBLIC_FIELD", "Public requests cannot contain credentials or private browser data.")
    title = _text(arguments.get("title"), "title", MAX_TITLE)
    body = _text(arguments.get("body"), "body", MAX_BODY)
    tags = _list(arguments.get("tags"), "tags", MAX_ITEMS)
    capabilities = _list(arguments.get("required_capabilities"), "required_capabilities", MAX_ITEMS)
    acceptance = _list(arguments.get("acceptance"), "acceptance", MAX_ITEMS)
    author = _text(arguments.get("author_agent", "anonymous-agent"), "author_agent", MAX_AGENT)
    return {
        "title": title,
        "body": body,
        "tags": tags,
        "required_capabilities": capabilities,
        "acceptance": acceptance,
        "author_agent": author,
    }


class AgentHelpBoard:
    """A local JSON provider with an optional HTTP provider.

    The HTTP service is intentionally a small contract adapter. It is not
    enabled by default, so offline tests never depend on a public deployment.
    """

    def __init__(self, state_path: str | Path, *, endpoint: str | None = None) -> None:
        self.state_path = Path(state_path).expanduser().resolve()
        self.endpoint = endpoint.strip().rstrip("/") if endpoint and endpoint.strip() else None
        self.provider = "http" if self.endpoint else "local"
        self._lock = threading.RLock()
        # Do not create the file on construction. A mock or read-only session
        # never posts a request, and an eager fsync here cost seconds per
        # startup on a slow disk while producing no useful state.

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema": "agent-help-board/v1", "requests": {}, "pending": {}}
        except (OSError, json.JSONDecodeError) as exc:
            raise BoardError("BOARD_STATE_INVALID", "The local Agent Help board cannot be read.", details={"reason": type(exc).__name__}) from None
        if not isinstance(data, dict) or data.get("schema") != "agent-help-board/v1":
            raise BoardError("BOARD_STATE_INVALID", "Unsupported Agent Help board state.")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.tmp")
        try:
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temp, self.state_path)
        except OSError as exc:
            raise BoardError("BOARD_STATE_WRITE_FAILED", "The local Agent Help board cannot be written.", details={"reason": type(exc).__name__}) from None
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _request_id() -> str:
        return "ahr_" + secrets.token_urlsafe(16).replace("-", "_").replace("=", "")

    @staticmethod
    def _confirmation(request_id: str) -> str:
        return "PUBLISH REQUEST " + request_id[-6:].upper()

    def _http(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        assert self.endpoint
        body = None
        headers = {
            "Accept": "application/json",
            "User-Agent": "ahan-agent-help-board/0.1",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.endpoint + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in {429, 500, 502, 503, 504}:
                raise BoardError("BOARD_HTTP_RETRYABLE", "The public board returned a retryable error.", retryable=True, details={"status": exc.code}) from None
            raise BoardError("BOARD_HTTP_FAILED", "The public board rejected the request.", details={"status": exc.code}) from None
        except (OSError, json.JSONDecodeError):
            raise BoardError("BOARD_HTTP_UNAVAILABLE", "The public board could not be reached.", retryable=True) from None

    def prepare(self, arguments: dict[str, Any]) -> dict[str, Any]:
        request = _public_request(arguments)
        request_id = self._request_id()
        prepared = {
            **request,
            "request_id": request_id,
            "status": "prepared",
            "created_at": _now(),
            "updated_at": _now(),
            "content_sha256": _digest(request),
            "provider": self.provider,
        }
        phrase = self._confirmation(request_id)
        if self.provider == "http":
            # The public provider receives the request only after confirmation.
            return {"request": prepared, "confirmation_phrase": phrase, "state": "prepared"}
        with self._lock:
            data = self._read()
            data.setdefault("pending", {})[request_id] = {"request": prepared, "confirmation_phrase": phrase}
            self._write(data)
        return {"request": prepared, "confirmation_phrase": phrase, "state": "prepared"}

    def publish(self, request_id: str, confirmation: str, prepared: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(request_id, str) or not request_id.startswith("ahr_"):
            raise BoardError("INVALID_ARGUMENT", "request_id is invalid.")
        if self.provider == "http":
            if not prepared or confirmation != self._confirmation(request_id):
                raise BoardError("CONFIRMATION_MISMATCH", "The request confirmation phrase does not match.")
            payload = {**prepared, "status": "open", "updated_at": _now()}
            try:
                result = self._http("POST", "/v1/requests", payload)
            except BoardError:
                raise
            return {"provider": "http", "request": result, "state": "open"}
        with self._lock:
            data = self._read()
            pending = data.get("pending", {}).get(request_id)
            if not isinstance(pending, dict):
                raise BoardError("REQUEST_NOT_FOUND", "The prepared request does not exist.")
            if confirmation != pending.get("confirmation_phrase"):
                raise BoardError("CONFIRMATION_MISMATCH", "The request confirmation phrase does not match.")
            request = dict(pending["request"])
            request["status"] = "open"
            request["updated_at"] = _now()
            data.setdefault("requests", {})[request_id] = request
            data.get("pending", {}).pop(request_id, None)
            self._write(data)
        return {"provider": "local", "request": request, "state": "open"}

    def list(self, *, limit: int = 20, tag: str | None = None, capability: str | None = None) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise BoardError("INVALID_ARGUMENT", "limit must be an integer from 1 to 50.")
        if self.provider == "http":
            query = f"?limit={limit}"
            if tag:
                query += "&tag=" + urllib.parse.quote(tag.strip().lower())
            if capability:
                query += "&capability=" + urllib.parse.quote(capability.strip().lower())
            result = self._http("GET", "/v1/requests" + query)
            return {"provider": "http", "requests": result.get("requests", result) if isinstance(result, dict) else result}
        with self._lock:
            records = list(self._read().get("requests", {}).values())
        records.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        if tag:
            records = [item for item in records if tag.lower() in item.get("tags", [])]
        if capability:
            records = [item for item in records if capability.lower() in item.get("required_capabilities", [])]
        return {"provider": "local", "requests": records[:limit], "count": min(len(records), limit)}

    def get(self, request_id: str) -> dict[str, Any]:
        if self.provider == "http":
            result = self._http("GET", "/v1/requests/" + request_id)
            return {"provider": "http", "request": result}
        with self._lock:
            request = self._read().get("requests", {}).get(request_id)
        if not isinstance(request, dict):
            raise BoardError("REQUEST_NOT_FOUND", "The public request does not exist.")
        return {"provider": "local", "request": request}
