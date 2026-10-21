"""Live-write path with a fake X transport: no network, no real token."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import XStarter  # noqa: E402
from x_api import XApiClient  # noqa: E402


class FakeX:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, bytes | None]] = []
        self.lock = threading.Lock()

    def __call__(self, method, url, headers, body, timeout):
        with self.lock:
            self.calls.append((method, url, body))
            item = self.responses.pop(0) if self.responses else (200, {}, b'{"data":{"id":"1"}}')
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item()
        return item


def _token_store(directory: Path) -> Path:
    path = directory / "tokens.json"
    path.write_text(
        json.dumps(
            {
                "schema": "x-starter-oauth/v1",
                "accounts": {
                    "default": {
                        "access_token": "fake-access",
                        "refresh_token": "",
                        "expires_at": time.time() + 3600,
                        "scope": "tweet.read tweet.write users.read",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


class LiveWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = {
            "X_STARTER_ENABLE_LIVE": "1",
            "X_STARTER_X_CLIENT_ID": "client-public-id",
            "X_STARTER_TOKEN_STORE": str(_token_store(self.dir)),
            "X_STARTER_STATE_DIR": str(self.dir / "state"),
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _app(self, fake: FakeX) -> XStarter:
        client = XApiClient(transport=fake, sleep=lambda _s: None)
        return XStarter(mode="live", environ=self.env, x_api=client)

    def _prepare(self, app: XStarter, text: str = "hello") -> dict:
        result = app.call_tool("x_post_prepare_api", {"text": text}, session_id="s1")
        self.assertTrue(result["ok"], result)
        return result["data"]

    def _create(self, app: XStarter, prepared: dict, session: str = "s1") -> dict:
        return app.call_tool(
            "x_post_create_api",
            {"write_id": prepared["write_id"], "confirmation": prepared["confirmation_phrase"]},
            session_id=session,
        )

    def test_success_marks_completed_and_persists(self) -> None:
        fake = FakeX([(201, {"x-rate-limit-remaining": "99"}, b'{"data":{"id":"42","text":"hello"}}')])
        app = self._app(fake)
        prepared = self._prepare(app)
        result = self._create(app, prepared)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["post_id"], "42")
        reloaded = XStarter(mode="live", environ=self.env)
        record = reloaded.api_writes.get(prepared["write_id"])
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["post_id"], "42")
        stored = (self.dir / "state" / "api_writes.json").read_text(encoding="utf-8")
        self.assertNotIn("fake-access", stored)

    def test_second_confirmation_is_rejected(self) -> None:
        fake = FakeX([(201, {}, b'{"data":{"id":"42"}}')])
        app = self._app(fake)
        prepared = self._prepare(app)
        self.assertTrue(self._create(app, prepared)["ok"])
        again = self._create(app, prepared)
        self.assertEqual(again["error"]["code"], "WRITE_NOT_READY")
        self.assertEqual(len(fake.calls), 1)

    def test_network_error_is_uncertain_and_not_retried(self) -> None:
        fake = FakeX([TimeoutError("timed out")])
        app = self._app(fake)
        prepared = self._prepare(app)
        result = self._create(app, prepared)
        self.assertEqual(result["error"]["code"], "X_WRITE_UNVERIFIED")
        self.assertFalse(result["error"]["retryable"])
        self.assertEqual(app.api_writes.get(prepared["write_id"])["state"], "uncertain")
        self.assertEqual(len(fake.calls), 1)

    def test_server_error_is_failed_without_retry(self) -> None:
        fake = FakeX([(503, {}, b'{"title":"Service Unavailable"}')])
        app = self._app(fake)
        prepared = self._prepare(app)
        result = self._create(app, prepared)
        self.assertEqual(result["error"]["code"], "X_UPSTREAM_ERROR")
        self.assertEqual(app.api_writes.get(prepared["write_id"])["state"], "failed")
        self.assertEqual(len(fake.calls), 1)

    def test_restart_during_execution_becomes_uncertain(self) -> None:
        app = self._app(FakeX([]))
        prepared = self._prepare(app)
        app.api_writes.transition(prepared["write_id"], "prepared", "executing")
        restarted = XStarter(mode="live", environ=self.env)
        record = restarted.api_writes.get(prepared["write_id"])
        self.assertEqual(record["state"], "uncertain")
        self.assertEqual(record["recovery_reason"], "process_restarted")

    def test_prepared_write_survives_restart(self) -> None:
        app = self._app(FakeX([]))
        prepared = self._prepare(app)
        fake = FakeX([(201, {}, b'{"data":{"id":"7"}}')])
        restarted = self._app(fake)
        result = self._create(restarted, prepared)
        self.assertTrue(result["ok"], result)

    def test_concurrent_confirmations_send_once(self) -> None:
        gate = threading.Event()

        def slow():
            gate.wait(2)
            return (201, {}, b'{"data":{"id":"9"}}')

        fake = FakeX([slow] * 8)
        app = self._app(fake)
        prepared = self._prepare(app)
        results: list[dict] = []
        threads = [
            threading.Thread(target=lambda: results.append(self._create(app, prepared)))
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        time.sleep(0.2)
        gate.set()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(results), 8)
        self.assertEqual(sum(1 for r in results if r["ok"]), 1)
        self.assertEqual(len(fake.calls), 1)

    def test_other_session_cannot_confirm(self) -> None:
        fake = FakeX([])
        app = self._app(fake)
        prepared = self._prepare(app)
        result = self._create(app, prepared, session="s2")
        self.assertEqual(result["error"]["code"], "WRITE_NOT_FOUND")
        self.assertEqual(fake.calls, [])

    def test_get_retries_transient_error_and_records_rate_limit(self) -> None:
        fake = FakeX(
            [
                (503, {}, b"{}"),
                (
                    200,
                    {"x-rate-limit-remaining": "5", "x-rate-limit-limit": "75", "x-rate-limit-reset": "1"},
                    b'{"data":{"id":"u1","username":"demo","name":"Demo"}}',
                ),
            ]
        )
        app = self._app(fake)
        trace: list = []
        payload = app._x_get("/2/users/me", {}, trace, action="read_account")
        self.assertEqual(payload["data"]["id"], "u1")
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(trace[-1]["detail"]["attempts"], 2)
        self.assertEqual(trace[-1]["detail"]["rate_limit"]["remaining"], 5)


if __name__ == "__main__":
    unittest.main()
