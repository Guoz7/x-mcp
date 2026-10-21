"""Standalone X API v2 client shared by the x-starter MCP server.

The module only uses the Python standard library. It exposes a transport
seam so tests can run without network access, and it never logs or embeds
the bearer token in error text.
"""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping

__all__ = [
    "RateLimit",
    "Transport",
    "XApiClient",
    "XApiError",
    "XApiResponse",
    "urllib_transport",
]

DEFAULT_BASE_URL = "https://api.x.com"

_MAX_DETAIL_CHARS = 300
_DETAIL_KEYS = ("detail", "title", "error")
_REDACTED = "<redacted>"


class XApiError(Exception):
    """A failure raised by :class:`XApiClient`.

    ``outcome`` is ``"failed"`` when the server definitely rejected the
    request and ``"unknown"`` when the request may or may not have been
    applied (network failure, or a 2xx response that could not be parsed).
    Callers must never re-send a request whose outcome is ``"unknown"``.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        status: int | None = None,
        details: Mapping[str, Any] | None = None,
        outcome: str = "failed",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status = status
        self.details: dict[str, Any] = dict(details or {})
        self.details.setdefault("status", status)
        self.outcome = outcome

    def __str__(self) -> str:
        # The message never contains credentials or request headers.
        return self.message

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, status={self.status!r}, "
            f"retryable={self.retryable!r}, outcome={self.outcome!r}, "
            f"message={self.message!r})"
        )


@dataclass(frozen=True)
class RateLimit:
    """Values parsed from the ``x-rate-limit-*`` response headers."""

    limit: int | None = None
    remaining: int | None = None
    reset_epoch: int | None = None

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> "RateLimit | None":
        lowered = _lowercase_headers(headers)
        limit = _optional_int(lowered.get("x-rate-limit-limit"))
        remaining = _optional_int(lowered.get("x-rate-limit-remaining"))
        reset_epoch = _optional_int(lowered.get("x-rate-limit-reset"))
        if limit is None and remaining is None and reset_epoch is None:
            return None
        return cls(limit=limit, remaining=remaining, reset_epoch=reset_epoch)

    def as_dict(self) -> dict[str, int | None]:
        return {
            "limit": self.limit,
            "remaining": self.remaining,
            "reset_epoch": self.reset_epoch,
        }


@dataclass
class XApiResponse:
    """A successful JSON response from the X API."""

    status: int
    payload: dict[str, Any]
    rate_limit: RateLimit | None
    attempts: int
    elapsed_ms: float


# (method, url, headers, body, timeout) -> (status, headers, body)
# Network failures raise OSError. Non-2xx statuses are returned as values.
Transport = Callable[
    [str, str, dict[str, str], "bytes | None", float],
    "tuple[int, dict[str, str], bytes]",
]


def urllib_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, dict[str, str], bytes]:
    """Default transport built on :mod:`urllib`.

    ``HTTPError`` (which covers every non-2xx status) is converted into a
    normal return value. Other ``OSError`` subclasses propagate.
    """

    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            response_headers = {str(key): str(value) for key, value in response.headers.items()}
            return status, response_headers, response.read()
    except urllib.error.HTTPError as exc:
        details = {str(key): str(value) for key, value in (exc.headers.items() if exc.headers else [])}
        try:
            payload = exc.read()
        except Exception:  # pragma: no cover - defensive against a broken stream
            payload = b""
        return int(exc.code), details, payload or b""


class XApiClient:
    """Small, testable X API client with explicit retry and outcome rules."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: Transport | None = None,
        user_agent: str = "x-starter/0.5",
        timeout: float = 15.0,
        max_get_attempts: int = 3,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_get_attempts < 1:
            raise ValueError("max_get_attempts must be >= 1")
        self.base_url = base_url
        self.transport: Transport = transport or urllib_transport
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_get_attempts = max_get_attempts
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.sleep = sleep
        self.clock = clock
        self._random = random.Random()
        self._lock = threading.Lock()
        self._rate_limits: dict[str, RateLimit] = {}

    # ------------------------------------------------------------------ public

    def get(self, path: str, query: Mapping[str, str] | None, token: str) -> XApiResponse:
        """GET ``path`` with bounded retries for 429, 5xx and network errors."""

        url = self._build_url(path, query)
        headers = self._request_headers(token, has_body=False)
        started = self.clock()
        attempt = 0
        while True:
            attempt += 1
            try:
                status, response_headers, raw_body = self.transport(
                    "GET", url, headers, None, self.timeout
                )
            except OSError as exc:
                if attempt < self.max_get_attempts:
                    self._sleep_backoff(attempt)
                    continue
                raise self._network_error(exc, token=token, attempts=attempt) from None

            rate_limit = self._capture_rate_limit(path, response_headers)
            if 200 <= status < 300:
                payload = self._decode_object(
                    raw_body,
                    status=status,
                    token=token,
                    outcome="failed",
                )
                return XApiResponse(
                    status=status,
                    payload=payload,
                    rate_limit=rate_limit,
                    attempts=attempt,
                    elapsed_ms=self._elapsed_ms(started),
                )
            if status == 429:
                wait_seconds = self._rate_limit_wait(rate_limit, response_headers, attempt)
                if attempt >= self.max_get_attempts or wait_seconds > self.backoff_cap:
                    raise self._http_error(
                        status,
                        raw_body,
                        rate_limit=rate_limit,
                        token=token,
                        attempts=attempt,
                        extra={"retry_after_seconds": wait_seconds},
                    )
                self.sleep(wait_seconds)
                continue
            if status >= 500 and attempt < self.max_get_attempts:
                self._sleep_backoff(attempt)
                continue
            raise self._http_error(
                status, raw_body, rate_limit=rate_limit, token=token, attempts=attempt
            )

    def post_json(self, path: str, body: Mapping[str, Any], token: str) -> XApiResponse:
        """POST a JSON body. Never retries; a failed write may still have landed."""

        url = self._build_url(path, None)
        headers = self._request_headers(token, has_body=True)
        encoded = json.dumps(dict(body), ensure_ascii=False).encode("utf-8")
        started = self.clock()
        try:
            status, response_headers, raw_body = self.transport(
                "POST", url, headers, encoded, self.timeout
            )
        except OSError as exc:
            reason = _redact(str(exc) or exc.__class__.__name__, token)
            raise XApiError(
                "X_NETWORK_ERROR",
                f"X API request failed: {reason}",
                retryable=True,
                status=None,
                details={"status": None},
                outcome="unknown",
            ) from None

        rate_limit = self._capture_rate_limit(path, response_headers)
        if 200 <= status < 300:
            payload = self._decode_object(
                raw_body,
                status=status,
                token=token,
                outcome="unknown",
            )
            return XApiResponse(
                status=status,
                payload=payload,
                rate_limit=rate_limit,
                attempts=1,
                elapsed_ms=self._elapsed_ms(started),
            )
        raise self._http_error(
            status, raw_body, rate_limit=rate_limit, token=token, attempts=1
        )

    def last_rate_limit(self, path: str) -> RateLimit | None:
        """Most recent rate-limit snapshot for ``path`` (thread safe)."""

        with self._lock:
            return self._rate_limits.get(path)

    # ----------------------------------------------------------------- internal

    def _build_url(self, path: str, query: Mapping[str, str] | None) -> str:
        base = self.base_url.rstrip("/")
        suffix = path if path.startswith("/") else "/" + path
        url = base + suffix
        if query:
            url += "?" + urllib.parse.urlencode(dict(query))
        return url

    def _request_headers(self, token: str, *, has_body: bool) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if has_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _capture_rate_limit(
        self, path: str, headers: Mapping[str, str]
    ) -> RateLimit | None:
        rate_limit = RateLimit.from_headers(headers)
        if rate_limit is not None:
            with self._lock:
                self._rate_limits[path] = rate_limit
        return rate_limit

    def _decode_object(
        self, raw_body: bytes, *, status: int, token: str, outcome: str
    ) -> dict[str, Any]:
        try:
            payload = json.loads(_as_bytes(raw_body).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise XApiError(
                "X_INVALID_RESPONSE",
                f"X API returned HTTP {status}: response body was not valid JSON",
                retryable=False,
                status=status,
                details={"status": status},
                outcome=outcome,
            ) from None
        if not isinstance(payload, dict):
            raise XApiError(
                "X_INVALID_RESPONSE",
                f"X API returned HTTP {status}: response body was not a JSON object",
                retryable=False,
                status=status,
                details={"status": status},
                outcome=outcome,
            )
        return payload

    def _http_error(
        self,
        status: int,
        raw_body: bytes,
        *,
        rate_limit: RateLimit | None,
        token: str,
        attempts: int,
        extra: Mapping[str, Any] | None = None,
    ) -> XApiError:
        code, retryable = _map_status(status)
        detail = _error_detail(raw_body, token=token)
        details: dict[str, Any] = {"status": status, "attempts": attempts}
        if rate_limit is not None:
            details["rate_limit"] = rate_limit.as_dict()
        if extra:
            details.update(extra)
        return XApiError(
            code,
            f"X API returned HTTP {status}: {detail}",
            retryable=retryable,
            status=status,
            details=details,
            outcome="failed",
        )

    def _network_error(self, exc: OSError, *, token: str, attempts: int) -> XApiError:
        reason = _redact(str(exc) or exc.__class__.__name__, token)
        return XApiError(
            "X_NETWORK_ERROR",
            f"X API request failed: {reason}",
            retryable=True,
            status=None,
            details={"status": None, "attempts": attempts},
            outcome="failed",
        )

    def _rate_limit_wait(
        self,
        rate_limit: RateLimit | None,
        headers: Mapping[str, str],
        attempt: int,
    ) -> float:
        if rate_limit is not None and rate_limit.reset_epoch is not None:
            remaining = rate_limit.reset_epoch - self.clock()
            if remaining > 0:
                return float(remaining)
        retry_after = _retry_after_seconds(headers)
        if retry_after is not None:
            return retry_after
        return self._backoff_wait(attempt)

    def _sleep_backoff(self, attempt: int) -> None:
        self.sleep(self._backoff_wait(attempt))

    def _backoff_wait(self, attempt: int) -> float:
        base = min(self.backoff_cap, self.backoff_base * float(2 ** (attempt - 1)))
        jittered = base * (1.0 + 0.25 * self._random.random())
        return min(self.backoff_cap, jittered)

    def _elapsed_ms(self, started: float) -> float:
        return (self.clock() - started) * 1000.0


def _map_status(status: int) -> tuple[str, bool]:
    if status == 401:
        return "X_AUTH_FAILED", False
    if status == 403:
        return "X_PERMISSION_DENIED", False
    if status == 404:
        return "X_RESOURCE_NOT_FOUND", False
    if status == 429:
        return "X_RATE_LIMITED", True
    if status >= 500:
        return "X_UPSTREAM_ERROR", True
    return "X_UPSTREAM_ERROR", False


def _as_bytes(raw_body: bytes) -> bytes:
    if isinstance(raw_body, bytes):
        return raw_body
    if isinstance(raw_body, bytearray):
        return bytes(raw_body)
    if isinstance(raw_body, str):
        return raw_body.encode("utf-8")
    return b""


def _lowercase_headers(headers: Mapping[str, str]) -> dict[str, str]:
    if not headers:
        return {}
    return {str(key).lower(): value for key, value in headers.items()}


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    raw = _lowercase_headers(headers).get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return seconds


def _error_detail(raw_body: bytes, *, token: str) -> str:
    text = ""
    body = _as_bytes(raw_body)
    if body:
        payload: Any = None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            for key in _DETAIL_KEYS:
                value = payload.get(key)
                if value:
                    text = str(value)
                    break
        if not text:
            text = body.decode("utf-8", "replace")
    text = " ".join(text.split())[:_MAX_DETAIL_CHARS]
    text = _redact(text, token)
    return text or "no detail"


def _redact(text: str, token: str) -> str:
    if token and token in text:
        return text.replace(token, _REDACTED)
    return text
