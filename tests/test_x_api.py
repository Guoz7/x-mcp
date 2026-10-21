from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from x_api import (  # noqa: E402
    RateLimit,
    XApiClient,
    XApiError,
    urllib_transport,
)


TOKEN = "SECRET-TOKEN-123"


class FakeClock:
    """Deterministic clock so retry math is reproducible."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class SleepRecorder:
    """Sleep stand-in that records waits and advances the fake clock."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.waits: list[float] = []
        self._clock = clock

    def __call__(self, seconds: float) -> None:
        self.waits.append(float(seconds))
        if self._clock is not None:
            self._clock.advance(seconds)


class FakeTransport:
    """Returns a preset sequence of results; records every call."""

    def __init__(self, *results: Any, default: Any = None) -> None:
        self.results = list(results)
        self.default = default
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, dict[str, str], bytes]:
        with self._lock:
            self.calls.append(
                {
                    "method": method,
                    "url": url,
                    "headers": dict(headers),
                    "body": body,
                    "timeout": timeout,
                }
            )
            if self.results:
                result = self.results.pop(0)
            else:
                result = self.default
        if result is None:
            raise AssertionError("transport called more times than expected")
        if isinstance(result, Exception):
            raise result
        return result


def json_response(
    status: int, payload: Any, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    return status, dict(headers or {}), json.dumps(payload).encode("utf-8")


def raw_response(
    status: int, raw: bytes, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    return status, dict(headers or {}), raw


def error_response(
    status: int, detail: str, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    return json_response(status, {"detail": detail}, headers)


def build_client(
    transport: FakeTransport,
    *,
    clock: FakeClock | None = None,
    sleep: SleepRecorder | None = None,
    **kwargs: Any,
) -> tuple[XApiClient, FakeClock, SleepRecorder]:
    clock = clock or FakeClock()
    sleep = sleep or SleepRecorder(clock)
    client = XApiClient(transport=transport, clock=clock, sleep=sleep, **kwargs)
    return client, clock, sleep


class GetSuccessTests(unittest.TestCase):
    def test_get_success_returns_payload_and_rate_limit(self) -> None:
        headers = {
            "x-rate-limit-limit": "15",
            "x-rate-limit-remaining": "14",
            "x-rate-limit-reset": "1700000010",
        }
        transport = FakeTransport(json_response(200, {"data": {"id": "1"}}, headers))
        client, _, _ = build_client(transport)

        response = client.get("/2/tweets", {"ids": "1"}, TOKEN)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload, {"data": {"id": "1"}})
        self.assertEqual(response.attempts, 1)
        self.assertGreaterEqual(response.elapsed_ms, 0.0)
        self.assertEqual(response.rate_limit, RateLimit(15, 14, 1700000010))
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], "https://api.x.com/2/tweets?ids=1")
        self.assertIsNone(call["body"])
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(call["headers"]["Accept"], "application/json")
        self.assertEqual(call["headers"]["User-Agent"], "x-starter/0.5")
        self.assertNotIn("Content-Type", call["headers"])

    def test_query_is_url_encoded(self) -> None:
        transport = FakeTransport(json_response(200, {"data": []}))
        client, _, _ = build_client(transport)

        client.get("/2/tweets/search/recent", {"q": "a b&c", "id": "1"}, TOKEN)

        self.assertEqual(
            transport.calls[0]["url"],
            "https://api.x.com/2/tweets/search/recent?q=a+b%26c&id=1",
        )

    def test_base_url_override_avoids_double_slash(self) -> None:
        transport = FakeTransport(json_response(200, {"ok": True}))
        client, _, _ = build_client(transport, base_url="https://api.x.com/")

        client.get("/2/users/me", None, TOKEN)

        self.assertEqual(transport.calls[0]["url"], "https://api.x.com/2/users/me")


class GetStatusTests(unittest.TestCase):
    def test_get_401_is_not_retried(self) -> None:
        transport = FakeTransport(error_response(401, "Unauthorized"))
        client, _, sleep = build_client(transport)

        with self.assertRaises(XApiError) as ctx:
            client.get("/2/users/me", None, TOKEN)

        error = ctx.exception
        self.assertEqual(error.code, "X_AUTH_FAILED")
        self.assertFalse(error.retryable)
        self.assertEqual(error.status, 401)
        self.assertEqual(error.outcome, "failed")
        self.assertEqual(error.message, "X API returned HTTP 401: Unauthorized")
        self.assertEqual(error.details["status"], 401)
        self.assertEqual(error.details["attempts"], 1)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(sleep.waits, [])

    def test_get_403_is_not_retried(self) -> None:
        transport = FakeTransport(error_response(403, "Forbidden"))
        client, _, _ = build_client(transport)

        with self.assertRaises(XApiError) as ctx:
            client.get("/2/users/me", None, TOKEN)

        self.assertEqual(ctx.exception.code, "X_PERMISSION_DENIED")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(len(transport.calls), 1)

    def test_get_404_is_not_retried(self) -> None:
        transport = FakeTransport(error_response(404, "Not Found"))
        client, _, _ = build_client(transport)

        with self.assertRaises(XApiError) as ctx:
            client.get("/2/tweets/999", None, TOKEN)

        self.assertEqual(ctx.exception.code, "X_RESOURCE_NOT_FOUND")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(len(transport.calls), 1)

    def test_get_400_maps_to_upstream_error_without_retry(self) -> None:
        transport = FakeTransport(error_response(400, "Bad Request"))
        client, _, _ = build_client(transport)

        with self.assertRaises(XApiError) as ctx:
            client.get("/2/tweets", None, TOKEN)

        self.assertEqual(ctx.exception.code, "X_UPSTREAM_ERROR")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(len(transport.calls), 1)

    def test_get_error_detail_prefers_json_fields(self) -> None:
        transport = FakeTransport(
            raw_response(422, json.dumps({"title": "Invalid", "detail": "bad id"}).encode())
        )
        client, _, _ = build_client(transport)

        with self.assertRaises(XApiError) as ctx:
            client.get("/2/tweets", None, TOKEN)

        self.assertEqual(ctx.exception.message, "X API returned HTTP 422: bad id")

    def test_get_non_json_error_detail_falls_back_to_body(self) -> None:
        transport = FakeTransport(raw_response(500, b"upstream exploded"))
        client, _, _ = build_client(transport, max_get_attempts=1)

        with self.assertRaises(XApiError) as ctx:
            client.get("/2/tweets", None, TOKEN)

        self.assertEqual(ctx.exception.message, "X API returned HTTP 500: upstream exploded")

    def test_get_invalid_json_success_body_is_not_retried(self) -> None:
        transport = FakeTransport(raw_response(200, b"<html>not json</html>"))
        client, _, _ = build_client(transport)

        with self.assertRaises(XApiError) as ctx:
            client.get("/2/tweets", None, TOKEN)

        self.assertEqual(ctx.exception.code, "X_INVALID_RESPONSE")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.status, 200)
        self.assertEqual(ctx.exception.details["status"], 200)
        self.assertEqual(len(transport.calls), 1)


class GetRetryTests(unittest.TestCase):
    def test_get_429_waits_for_reset_then_succeeds(self) -> None:
        reset_headers = {
            "x-rate-limit-limit": "15",
            "x-rate-limit-remaining": "0",
        }
        clock = FakeClock(1000.0)
        transport = FakeTransport(
            error_response(429, "Too Many Requests", {**reset_headers, "x-rate-limit-reset": "1005"}),
            json_response(200, {"data": {"id": "1"}}),
        )
        client, _, sleep = build_client(transport, clock=clock)

        response = client.get("/2/tweets", None, TOKEN)

        self.assertEqual(response.attempts, 2)
        self.assertEqual(sleep.waits, [5.0])
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(clock.now, 1005.0)

    def test_get_429_uses_retry_after_when_reset_missing(self) -> None:
        transport = FakeTransport(
            error_response(429, "Too Many Requests", {"retry-after": "3"}),
            json_response(200, {"data": {"id": "1"}}),
        )
        client, _, sleep = build_client(transport)

        response = client.get("/2/tweets", None, TOKEN)

        self.assertEqual(response.attempts, 2)
        self.assertEqual(sleep.waits, [3.0])

    def test_get_429_wait_over_cap_fails_without_sleeping(self) -> None:
        transport = FakeTransport(
            error_response(429, "Too Many Requests", {"x-rate-limit-reset": "1060"})
        )
        client, _, sleep = build_client(transport, clock=FakeClock(1000.0), backoff_cap=8.0)

        with self.assertRaises(XApiError) as ctx:
            client.get("/2/tweets", None, TOKEN)

        error = ctx.exception
        self.assertEqual(error.code, "X_RATE_LIMITED")
        self.assertTrue(error.retryable)
        self.assertEqual(error.outcome, "failed")
        self.assertAlmostEqual(error.details["retry_after_seconds"], 60.0, places=3)
        self.assertEqual(sleep.waits, [])
        self.assertEqual(len(transport.calls), 1)

    def test_get_429_exhausts_attempts(self) -> None:
        transport = FakeTransport(
            *[error_response(429, "Too Many Requests", {"retry-after": "1"})] * 4
        )
        client, _, sleep = build_client(transport, max_get_attempts=3)

        with self.assertRaises(XApiError) as ctx:
            client.get("/2/tweets", None, TOKEN)

        self.assertEqual(ctx.exception.code, "X_RATE_LIMITED")
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(sleep.waits, [1.0, 1.0])

    def test_get_503_retries_then_succeeds(self) -> None:
        transport = FakeTransport(
            error_response(503, "Service Unavailable"),
            json_response(200, {"data": {"id": "1"}}),
        )
        client, _, sleep = build_client(transport, backoff_base=0.5, backoff_cap=8.0)

        response = client.get("/2/tweets", None, TOKEN)

        self.assertEqual(response.attempts, 2)
        self.assertEqual(len(sleep.waits), 1)
        self.assertGreaterEqual(sleep.waits[0], 0.5)
        self.assertLessEqual(sleep.waits[0], 0.625)

    def test_get_503_exhausts_attempts(self) -> None:
        transport = FakeTransport(
            *[error_response(503, "Service Unavailable")] * 3,
        )
        client, _, sleep = build_client(transport, max_get_attempts=3)

        with self.assertRaises(XApiError) as ctx:
            client.get("/2/tweets", None, TOKEN)

        error = ctx.exception
        self.assertEqual(error.code, "X_UPSTREAM_ERROR")
        self.assertTrue(error.retryable)
        self.assertEqual(error.status, 503)
        self.assertEqual(error.outcome, "failed")
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(len(sleep.waits), 2)

    def test_get_backoff_grows_and_stays_capped(self) -> None:
        transport = FakeTransport(*[error_response(500, "Boom")] * 4)
        client, _, sleep = build_client(
            transport, max_get_attempts=4, backoff_base=4.0, backoff_cap=5.0
        )

        with self.assertRaises(XApiError):
            client.get("/2/tweets", None, TOKEN)

        self.assertEqual(len(transport.calls), 4)
        self.assertEqual(len(sleep.waits), 3)
        for wait in sleep.waits:
            self.assertLessEqual(wait, 5.0)
            self.assertGreaterEqual(wait, 4.0)

    def test_get_network_error_retries_then_succeeds(self) -> None:
        transport = FakeTransport(
            OSError("connection reset by peer"),
            json_response(200, {"data": {"id": "1"}}),
        )
        client, _, sleep = build_client(transport)

        response = client.get("/2/tweets", None, TOKEN)

        self.assertEqual(response.attempts, 2)
        self.assertEqual(len(sleep.waits), 1)

    def test_get_network_error_exhausts_attempts(self) -> None:
        transport = FakeTransport(*[OSError("dns failure")] * 3)
        client, _, _ = build_client(transport, max_get_attempts=3)

        with self.assertRaises(XApiError) as ctx:
            client.get("/2/tweets", None, TOKEN)

        error = ctx.exception
        self.assertEqual(error.code, "X_NETWORK_ERROR")
        self.assertTrue(error.retryable)
        self.assertIsNone(error.status)
        self.assertIsNone(error.details["status"])
        self.assertEqual(len(transport.calls), 3)

    def test_get_timeout_is_a_retryable_network_error(self) -> None:
        transport = FakeTransport(*[TimeoutError("timed out")] * 3)
        client, _, _ = build_client(transport)

        with self.assertRaises(XApiError) as ctx:
            client.get("/2/tweets", None, TOKEN)

        self.assertEqual(ctx.exception.code, "X_NETWORK_ERROR")
        self.assertTrue(ctx.exception.retryable)

    def test_max_get_attempts_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            XApiClient(transport=FakeTransport(), max_get_attempts=0)


class PostTests(unittest.TestCase):
    def test_post_encodes_json_body_and_headers(self) -> None:
        transport = FakeTransport(json_response(201, {"data": {"id": "9", "text": "hi there"}}))
        client, _, _ = build_client(transport)

        response = client.post_json("/2/tweets", {"text": "hi there"}, TOKEN)

        self.assertEqual(response.status, 201)
        self.assertEqual(response.payload["data"]["id"], "9")
        self.assertEqual(response.attempts, 1)
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://api.x.com/2/tweets")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(
            call["body"], json.dumps({"text": "hi there"}, ensure_ascii=False).encode("utf-8")
        )

    def test_post_keeps_unicode_body_unencoded(self) -> None:
        transport = FakeTransport(json_response(201, {"data": {"id": "9"}}))
        client, _, _ = build_client(transport)

        client.post_json("/2/tweets", {"text": "hello \u4e16\u754c"}, TOKEN)

        self.assertEqual(
            transport.calls[0]["body"],
            json.dumps({"text": "hello \u4e16\u754c"}, ensure_ascii=False).encode("utf-8"),
        )

    def test_post_503_does_not_retry_and_reports_failed(self) -> None:
        transport = FakeTransport(error_response(503, "Service Unavailable"))
        client, _, sleep = build_client(transport)

        with self.assertRaises(XApiError) as ctx:
            client.post_json("/2/tweets", {"text": "hi"}, TOKEN)

        error = ctx.exception
        self.assertEqual(error.code, "X_UPSTREAM_ERROR")
        self.assertTrue(error.retryable)
        self.assertEqual(error.outcome, "failed")
        self.assertEqual(error.status, 503)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(sleep.waits, [])

    def test_post_401_reports_auth_failed_failed_outcome(self) -> None:
        transport = FakeTransport(error_response(401, "Unauthorized"))
        client, _, _ = build_client(transport)

        with self.assertRaises(XApiError) as ctx:
            client.post_json("/2/tweets", {"text": "hi"}, TOKEN)

        self.assertEqual(ctx.exception.code, "X_AUTH_FAILED")
        self.assertEqual(ctx.exception.outcome, "failed")
        self.assertEqual(len(transport.calls), 1)

    def test_post_network_error_reports_unknown_after_one_call(self) -> None:
        transport = FakeTransport(OSError("connection aborted"))
        client, _, sleep = build_client(transport)

        with self.assertRaises(XApiError) as ctx:
            client.post_json("/2/tweets", {"text": "hi"}, TOKEN)

        error = ctx.exception
        self.assertEqual(error.code, "X_NETWORK_ERROR")
        self.assertTrue(error.retryable)
        self.assertEqual(error.outcome, "unknown")
        self.assertIsNone(error.status)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(sleep.waits, [])

    def test_post_timeout_reports_unknown(self) -> None:
        transport = FakeTransport(TimeoutError("timed out"))
        client, _, _ = build_client(transport)

        with self.assertRaises(XApiError) as ctx:
            client.post_json("/2/tweets", {"text": "hi"}, TOKEN)

        self.assertEqual(ctx.exception.outcome, "unknown")
        self.assertEqual(len(transport.calls), 1)

    def test_post_2xx_non_json_reports_unknown(self) -> None:
        transport = FakeTransport(raw_response(200, b"<html>proxy</html>"))
        client, _, _ = build_client(transport)

        with self.assertRaises(XApiError) as ctx:
            client.post_json("/2/tweets", {"text": "hi"}, TOKEN)

        error = ctx.exception
        self.assertEqual(error.code, "X_INVALID_RESPONSE")
        self.assertEqual(error.outcome, "unknown")
        self.assertEqual(error.status, 200)
        self.assertFalse(error.retryable)
        self.assertEqual(len(transport.calls), 1)

    def test_post_2xx_json_array_reports_unknown(self) -> None:
        transport = FakeTransport(json_response(200, [1, 2, 3]))
        client, _, _ = build_client(transport)

        with self.assertRaises(XApiError) as ctx:
            client.post_json("/2/tweets", {"text": "hi"}, TOKEN)

        self.assertEqual(ctx.exception.code, "X_INVALID_RESPONSE")
        self.assertEqual(ctx.exception.outcome, "unknown")
        self.assertEqual(len(transport.calls), 1)


class RateLimitHeaderTests(unittest.TestCase):
    def test_rate_limit_from_headers_is_case_insensitive(self) -> None:
        parsed = RateLimit.from_headers(
            {
                "X-Rate-Limit-Limit": "100",
                "x-RATE-LIMIT-Remaining": "0",
                "X-Rate-Limit-Reset": "1700000000",
            }
        )

        self.assertEqual(parsed, RateLimit(100, 0, 1700000000))
        self.assertEqual(
            parsed.as_dict(), {"limit": 100, "remaining": 0, "reset_epoch": 1700000000}
        )

    def test_rate_limit_missing_returns_none(self) -> None:
        self.assertIsNone(RateLimit.from_headers({}))
        self.assertIsNone(RateLimit.from_headers({"content-type": "application/json"}))
        self.assertIsNone(RateLimit.from_headers({"x-rate-limit-limit": "not-a-number"}))

    def test_rate_limit_partial_headers_keep_none_fields(self) -> None:
        parsed = RateLimit.from_headers({"x-rate-limit-remaining": "5"})

        self.assertEqual(parsed, RateLimit(None, 5, None))
        self.assertEqual(parsed.as_dict(), {"limit": None, "remaining": 5, "reset_epoch": None})

    def test_last_rate_limit_records_success_and_error_responses(self) -> None:
        future_reset = int(1_700_000_000.0 + 600)
        headers = {
            "x-rate-limit-limit": "50",
            "x-rate-limit-remaining": "0",
            "x-rate-limit-reset": str(future_reset),
        }
        transport = FakeTransport(error_response(429, "Too Many Requests", headers))
        client, _, _ = build_client(transport)

        with self.assertRaises(XApiError):
            client.get("/2/tweets", None, TOKEN)

        self.assertEqual(client.last_rate_limit("/2/tweets"), RateLimit(50, 0, future_reset))
        self.assertIsNone(client.last_rate_limit("/2/users/me"))
        transport2 = FakeTransport(json_response(200, {"ok": True}, headers))
        client2, _, _ = build_client(transport2)
        client2.get("/2/users/me", None, TOKEN)
        self.assertEqual(client2.last_rate_limit("/2/users/me"), RateLimit(50, 0, future_reset))

    def test_last_rate_limit_is_not_overwritten_by_headerless_response(self) -> None:
        transport = FakeTransport(
            json_response(200, {"ok": True}, {"x-rate-limit-remaining": "9"}),
            json_response(200, {"ok": True}, {}),
        )
        client, _, _ = build_client(transport)

        client.get("/2/tweets", None, TOKEN)
        client.get("/2/tweets", None, TOKEN)

        self.assertEqual(client.last_rate_limit("/2/tweets"), RateLimit(None, 9, None))


class SecretHygieneTests(unittest.TestCase):
    def test_error_message_and_repr_never_contain_the_token(self) -> None:
        transport = FakeTransport(
            error_response(401, f"bad token {TOKEN} rejected"),
        )
        client, _, _ = build_client(transport)

        with self.assertRaises(XApiError) as ctx:
            client.get("/2/users/me", None, TOKEN)

        error = ctx.exception
        self.assertNotIn(TOKEN, str(error))
        self.assertNotIn(TOKEN, repr(error))
        self.assertIn("<redacted>", error.message)
        self.assertNotIn(TOKEN, json.dumps(error.details, default=str))

    def test_network_error_message_never_contains_the_token(self) -> None:
        transport = FakeTransport(*[OSError(f"proxy rejected {TOKEN}")] * 3)
        client, _, _ = build_client(transport)

        with self.assertRaises(XApiError) as ctx:
            client.get("/2/users/me", None, TOKEN)

        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertNotIn(TOKEN, repr(ctx.exception))
        self.assertIn("<redacted>", ctx.exception.message)


class ConcurrencyTests(unittest.TestCase):
    def test_twenty_threads_share_the_client_safely(self) -> None:
        headers = {"x-rate-limit-limit": "15", "x-rate-limit-remaining": "7"}
        transport = FakeTransport(
            json_response(200, {"data": {"id": "1"}}, headers),
            default=json_response(200, {"data": {"id": "1"}}, headers),
        )
        client, _, _ = build_client(transport)
        errors: list[BaseException] = []
        responses: list[Any] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                for _ in range(5):
                    result = client.get("/2/tweets", {"ids": "1"}, TOKEN)
                    with lock:
                        responses.append(result)
            except BaseException as exc:  # pragma: no cover - failure reporting
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(len(responses), 100)
        self.assertTrue(all(item.status == 200 for item in responses))
        self.assertTrue(all(item.rate_limit == RateLimit(15, 7, None) for item in responses))
        self.assertEqual(client.last_rate_limit("/2/tweets"), RateLimit(15, 7, None))
        self.assertEqual(len(transport.calls), 100)


class TransportTests(unittest.TestCase):
    """Exercises the default urllib transport against a loopback server."""

    def setUp(self) -> None:
        test_case = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                if self.path == "/boom":
                    body = json.dumps({"detail": "nope"}).encode("utf-8")
                    self.send_response(429)
                    self.send_header("x-rate-limit-reset", "1700000000")
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:  # silence test server logs
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

        def serve() -> None:
            try:
                self.server.serve_forever()
            except OSError:  # pragma: no cover - shutdown race on Windows
                pass

        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        test_case.addCleanup(self.server.shutdown)
        test_case.addCleanup(self.server.server_close)

    def test_urllib_transport_returns_status_and_headers(self) -> None:
        host, port = self.server.server_address[:2]
        status, headers, body = urllib_transport(
            "GET", f"http://{host}:{port}/ok", {"Accept": "application/json"}, None, 5.0
        )

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8")), {"ok": True})
        self.assertTrue(any(key.lower() == "content-type" for key in headers))

    def test_urllib_transport_converts_http_error_to_value(self) -> None:
        host, port = self.server.server_address[:2]
        status, headers, body = urllib_transport(
            "GET", f"http://{host}:{port}/boom", {"Accept": "application/json"}, None, 5.0
        )

        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body.decode("utf-8")), {"detail": "nope"})
        lowered = {key.lower(): value for key, value in headers.items()}
        self.assertEqual(lowered.get("x-rate-limit-reset"), "1700000000")

if __name__ == "__main__":
    unittest.main()
