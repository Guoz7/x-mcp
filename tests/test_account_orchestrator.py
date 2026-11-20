import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from account_orchestrator import (  # noqa: E402
    ContentQueueStore,
    OrchestratorError,
    build_daily_orchestration_plan,
    evaluate_account_health,
    evaluate_circuit_breaker,
)
from server import StarterError, XStarter, handle_message  # noqa: E402


def _action(
    action_id: str,
    action_type: str,
    *,
    status: str = "completed",
    completed_at: str = "2026-11-20T10:00:00+08:00",
    payload: dict | None = None,
    result: dict | None = None,
    error: dict | None = None,
) -> dict:
    return {
        "action_id": action_id,
        "type": action_type,
        "status": status,
        "created_at": completed_at,
        "completed_at": completed_at,
        "payload": payload or {},
        "result": result,
        "error": error,
    }


class CircuitBreakerAndHealthTests(unittest.TestCase):
    def test_suspension_code_141_blocks_all_writes(self) -> None:
        ledger = {
            "account_binding": "guoz7777",
            "actions": {
                "a1": _action(
                    "a1",
                    "like_post",
                    status="failed",
                    error={"details": {"rejection": {"code": 141, "message": "User is suspended"}}},
                )
            },
        }
        cb = evaluate_circuit_breaker(ledger, now_epoch=1800000000.0)
        self.assertEqual(cb["status"], "suspended")
        self.assertFalse(cb["allow_write"])
        self.assertEqual(cb["reason"], "ACCOUNT_SUSPENDED")

    def test_refusal_283_triggers_24h_cooldown_then_recovers(self) -> None:
        # 2026-11-20T10:00:00+08:00 == 2026-11-20T02:00:00Z
        from datetime import datetime, timezone

        refusal_epoch = datetime(2026, 11, 20, 2, 0, 0, tzinfo=timezone.utc).timestamp()
        ledger = {
            "account_binding": "guoz7777",
            "actions": {
                "a1": _action(
                    "a1",
                    "like_post",
                    status="failed",
                    completed_at="2026-11-20T02:00:00+00:00",
                    error={"details": {"rejection": {"code": 283, "message": "scarecrow policy block"}}},
                )
            },
        }
        during = evaluate_circuit_breaker(ledger, now_epoch=refusal_epoch + 3600)
        self.assertEqual(during["status"], "cooldown")
        self.assertFalse(during["allow_write"])
        self.assertEqual(during["reason"], "REFUSAL_COOLDOWN")

        after = evaluate_circuit_breaker(ledger, now_epoch=refusal_epoch + 25 * 3600)
        self.assertEqual(after["status"], "active")
        self.assertTrue(after["allow_write"])

    def test_uncertain_streak_triggers_cooldown(self) -> None:
        from datetime import datetime, timezone

        t0 = datetime(2026, 11, 20, 2, 0, 0, tzinfo=timezone.utc).timestamp()
        ledger = {
            "account_binding": "guoz7777",
            "actions": {
                "a1": _action("a1", "follow_profile", status="uncertain", completed_at="2026-11-20T01:55:00+00:00"),
                "a2": _action("a2", "follow_profile", status="uncertain", completed_at="2026-11-20T02:00:00+00:00"),
            },
        }
        cb = evaluate_circuit_breaker(ledger, now_epoch=t0 + 600)
        self.assertEqual(cb["status"], "cooldown")
        self.assertFalse(cb["allow_write"])
        self.assertEqual(cb["reason"], "UNCERTAIN_STREAK_COOLDOWN")

    def test_account_health_computes_score_warmup_and_safe_remaining(self) -> None:
        from datetime import datetime, timezone

        now = datetime(2026, 11, 20, 4, 0, 0, tzinfo=timezone.utc).timestamp()
        ledger = {
            "account_binding": "guoz7777",
            "actions": {
                "l1": _action(
                    "l1",
                    "like_post",
                    completed_at="2026-11-20T03:00:00+00:00",
                    payload={"post_url": "https://x.com/alice/status/123", "author": "alice"},
                    result={"post_url": "https://x.com/alice/status/123", "author": "alice", "state": "liked", "verified": True, "changed": True},
                )
            },
        }
        health = evaluate_account_health(
            ledger,
            account_snapshot={"display_name": "Guoz", "bio": "Builder", "has_avatar": True, "posts_count": 12, "account_age_days": 14},
            now_epoch=now,
        )
        self.assertEqual(health["account_status"], "active")
        self.assertTrue(health["warmup"]["ready"])
        self.assertGreaterEqual(health["standing_score"], 80)
        self.assertEqual(health["today"]["achieved"]["like_post"], 1)
        self.assertEqual(health["today"]["remaining_safe"]["like_post"], 59)
        self.assertEqual(health["today"]["remaining_safe"]["follow_profile"], 25)


class ContentQueueAndPlannerTests(unittest.TestCase):
    def test_content_queue_lifecycle_and_secret_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ContentQueueStore(tmp)
            with self.assertRaises(OrchestratorError) as caught:
                store.add(kind="original_post", text="token sk-1234567890123456789012345")
            self.assertEqual(caught.exception.code, "SECRET_DETECTED")

            future = time.time() + 3600
            item1 = store.add(kind="original_post", text="Shipped safe pacing today.", topic="build")
            item2 = store.add(kind="original_post", text="Later post.", scheduled_after_epoch=future)
            due = store.due()
            self.assertEqual([i["item_id"] for i in due], [item1["item_id"]])

            store.mark_published(item1["item_id"], published_url="https://x.com/guoz7777/status/999")
            self.assertEqual(len(store.list(status="published")), 1)
            store.remove(item2["item_id"])
            self.assertEqual(len(store.list(status="queued")), 0)

    def test_daily_plan_deduplicates_and_interleaves(self) -> None:
        ledger = {
            "account_binding": "guoz7777",
            "actions": {
                "f1": _action(
                    "f1",
                    "follow_profile",
                    payload={"handle": "already_followed", "profile_url": "https://x.com/already_followed"},
                    result={"handle": "already_followed", "profile_url": "https://x.com/already_followed", "state": "following", "verified": True, "changed": True},
                ),
                "l1": _action(
                    "l1",
                    "like_post",
                    payload={"post_url": "https://x.com/bob/status/100", "author": "bob"},
                    result={"post_url": "https://x.com/bob/status/100", "author": "bob", "state": "liked", "verified": True, "changed": True},
                ),
            },
        }
        plan = build_daily_orchestration_plan(
            ledger,
            goals={"publish_post": 1, "like_post": 2, "follow_profile": 10, "repost_post": 1, "reply_post": 1},
            content_queue=[
                {"item_id": "cq_1", "kind": "original_post", "status": "queued", "text": "Daily builder log"},
                {"item_id": "cq_2", "kind": "reply", "status": "queued", "text": "Great point!", "target_url": "https://x.com/eve/status/103"},
            ],
            timeline_posts=[
                {"url": "https://x.com/bob/status/100", "author": "bob", "text": "dup"},
                {"url": "https://x.com/carol/status/101", "author": "carol", "text": "post 1"},
                {"url": "https://x.com/dave/status/102", "author": "dave", "text": "post 2"},
                {"url": "https://x.com/eve/status/103", "author": "eve", "text": "post 3"},
                {"url": "https://x.com/frank/status/104", "author": "frank", "text": "post 4"},
            ],
            follow_candidates=[
                {"handle": "already_followed"},
                {"handle": "big_account", "followers_count": 50000, "following_count": 100},
                {"handle": "new_friend_1", "mutual_verified": True, "bio": "builder", "followers_count": 300, "following_count": 280},
                {"handle": "new_friend_2", "followers_count": 200, "following_count": 250},
            ],
            seed=42,
        )
        self.assertFalse(plan["blocked"])
        self.assertEqual(plan["status"], "planned_not_executed")
        # Warmup stage (posts < 10) caps follow_profile at 5 even though 10 were requested.
        self.assertEqual(plan["stage"], "warmup")
        self.assertEqual(plan["counts"], {"publish_post": 1, "like_post": 2, "follow_profile": 3, "repost_post": 1, "reply_post": 1})
        urls = [op.get("url") for op in plan["operations"] if op.get("url")]
        self.assertNotIn("https://x.com/bob/status/100", urls)
        handles = [op.get("handle") for op in plan["operations"] if op.get("handle")]
        self.assertNotIn("already_followed", handles)
        # Highest-scored candidate (new_friend_1) must come before big_account.
        self.assertLess(handles.index("new_friend_1"), handles.index("big_account"))


class DailyOrchestratorMcpTests(unittest.TestCase):
    def test_mcp_daily_orchestration_prepare_execute_and_refusal_halt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = XStarter(
                mode="browser",
                environ={
                    "X_STARTER_MODE": "browser",
                    "X_STARTER_ENABLE_BROWSER": "1",
                    "X_STARTER_STATE_DIR": tmp,
                },
            )
            # Seed account binding on the bridge ledger for publish_post verification.
            with app._bridge()._lock:
                state = app._bridge()._read()
                state["account_binding"] = "guoz7777"
                app._bridge()._write(state)

            # 1. Queue an original post via MCP.
            q = app.call_tool("x_content_queue", {"action": "add", "kind": "original_post", "text": "Automating X safely."})
            self.assertTrue(q["ok"])
            item_id = q["data"]["item"]["item_id"]

            # 2. Check health.
            h = app.call_tool("x_account_health", {})
            self.assertTrue(h["ok"])
            self.assertEqual(h["data"]["content_queue"]["due"], 1)

            # 3. Plan daily wave.
            planned = app.call_tool(
                "x_daily_orchestrate_plan",
                {
                    "goals": {"publish_post": 1, "like_post": 1, "follow_profile": 1, "repost_post": 0},
                    "timeline_posts": [{"url": "https://x.com/alice/status/555", "author": "alice", "text": "hello"}],
                    "follow_candidates": [{"handle": "alice"}],
                    "seed": 1,
                },
            )
            self.assertTrue(planned["ok"])
            self.assertEqual(planned["data"]["operation_count"], 3)

            # 4. Prepare confirmation.
            prep = app.call_tool("x_daily_orchestrate_prepare", {"plan": planned["data"]})
            self.assertTrue(prep["ok"])
            batch_id = prep["data"]["batch_id"]
            phrase = prep["data"]["confirmation_phrase"]

            # 5. Mock browser executor matching Browser.execute_x_action's exact signature.
            mock_browser = MagicMock()
            mock_browser.browser_family = "chromium"

            def fake_publish(*, text, expected_username, action_id, claim_token, lease_expires_epoch):
                import hashlib
                return {
                    "post_url": "https://x.com/guoz7777/status/777",
                    "author": "guoz7777",
                    "text": text,
                    "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "verified": True,
                    "changed": True,
                }

            def fake_exec(*, action_type, post_url, author, text, action_id, claim_token, lease_expires_epoch):
                if action_type == "like_post":
                    return {"post_url": post_url, "author": author, "state": "liked", "verified": True, "changed": True}
                if action_type == "follow_profile":
                    return {"profile_url": post_url, "handle": author, "state": "following", "verified": True, "changed": True}
                return {}

            mock_browser.execute_x_publish.side_effect = fake_publish
            mock_browser.execute_x_action.side_effect = fake_exec
            app._browser_for = lambda _args: mock_browser

            exe = app.call_tool(
                "x_daily_orchestrate_execute",
                {"batch_id": batch_id, "confirmation": phrase, "respect_pacing": False},
            )
            self.assertTrue(exe["ok"])
            self.assertEqual(exe["data"]["verified_changed"], 3)
            self.assertEqual(exe["data"]["status"], "completed")

            # Queue item was marked published.
            due_after = app.call_tool("x_content_queue", {"action": "due"})
            self.assertEqual(due_after["data"]["count"], 0)

    def test_refusal_during_daily_execute_trips_circuit_breaker(self) -> None:
        from browser_executor import ExecutorError

        with tempfile.TemporaryDirectory() as tmp:
            app = XStarter(
                mode="browser",
                environ={
                    "X_STARTER_MODE": "browser",
                    "X_STARTER_ENABLE_BROWSER": "1",
                    "X_STARTER_STATE_DIR": tmp,
                },
            )
            planned = app.call_tool(
                "x_daily_orchestrate_plan",
                {
                    "goals": {"publish_post": 0, "like_post": 2, "follow_profile": 0, "repost_post": 0},
                    "timeline_posts": [
                        {"url": "https://x.com/alice/status/555", "author": "alice", "text": "one"},
                        {"url": "https://x.com/bob/status/556", "author": "bob", "text": "two"},
                    ],
                    "seed": 1,
                },
            )
            prep = app.call_tool("x_daily_orchestrate_prepare", {"plan": planned["data"]})
            mock_browser = MagicMock()
            mock_browser.browser_family = "chromium"
            mock_browser.execute_x_action.side_effect = ExecutorError(
                "X_WRITE_REJECTED",
                "scarecrow policy block",
                details={"rejection": {"code": 283, "message": "scarecrow policy block"}},
            )
            app._browser_for = lambda _args: mock_browser

            exe = app.call_tool(
                "x_daily_orchestrate_execute",
                {
                    "batch_id": prep["data"]["batch_id"],
                    "confirmation": prep["data"]["confirmation_phrase"],
                    "respect_pacing": False,
                    "failure_policy": "continue",
                },
            )
            self.assertTrue(exe["ok"])
            self.assertEqual(exe["data"]["processed"], 1)
            self.assertEqual(exe["data"]["stop_reason"], "REFUSAL_CIRCUIT_BREAKER_POLICY")

            # Subsequent prepare must now be blocked by the 24h circuit breaker.
            blocked_prep = app.call_tool("x_daily_orchestrate_prepare", {"plan": planned["data"]})
            self.assertFalse(blocked_prep["ok"])
            self.assertEqual(blocked_prep["error"]["code"], "ACCOUNT_COOLDOWN")

    def test_lightweight_dashboard_http_routes(self) -> None:
        import json
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from server import make_http_handler

        with tempfile.TemporaryDirectory() as tmp:
            app = XStarter(
                mode="browser",
                environ={
                    "X_STARTER_MODE": "browser",
                    "X_STARTER_ENABLE_BROWSER": "1",
                    "X_STARTER_STATE_DIR": tmp,
                },
            )
            srv = ThreadingHTTPServer(("127.0.0.1", 0), make_http_handler(app))
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                html = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode("utf-8")
                self.assertIn("X MCP 轻量控制台", html)

                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/tool",
                    data=json.dumps({"tool": "x_account_health", "arguments": {}}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                resp = json.loads(urllib.request.urlopen(req).read().decode("utf-8"))
                self.assertTrue(resp["ok"])
                self.assertEqual(resp["data"]["account_status"], "active")
            finally:
                srv.shutdown()
                srv.server_close()

    def test_autopilot_window_run_wave_and_ops_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = XStarter(
                mode="browser",
                environ={
                    "X_STARTER_MODE": "browser",
                    "X_STARTER_ENABLE_BROWSER": "1",
                    "X_STARTER_STATE_DIR": tmp,
                },
            )
            prep = app.call_tool("x_autopilot", {"action": "prepare", "days": 7, "max_per_wave": 2})
            self.assertTrue(prep["ok"])
            act = app.call_tool(
                "x_autopilot",
                {
                    "action": "activate",
                    "window_id": prep["data"]["window_id"],
                    "confirmation": prep["data"]["confirmation_phrase"],
                },
            )
            self.assertTrue(act["ok"])
            self.assertTrue(act["data"]["enabled"])

            mock_browser = MagicMock()
            mock_browser.browser_family = "chromium"
            mock_browser.execute_x_action.side_effect = lambda *, action_type, post_url, author, text, action_id, claim_token, lease_expires_epoch: (
                {"post_url": post_url, "author": author, "state": "liked", "verified": True, "changed": True}
                if action_type == "like_post"
                else {"profile_url": post_url, "handle": author, "state": "following", "verified": True, "changed": True, "mutual_verified": True}
            )
            app._browser_for = lambda _args: mock_browser

            wave = app.call_tool(
                "x_autopilot",
                {
                    "action": "run_wave",
                    "force": True,
                    "respect_pacing": False,
                    "timeline_posts": [{"url": "https://x.com/alice/status/888", "author": "alice", "text": "hi"}],
                    "follow_candidates": [{"handle": "bob", "mutual_verified": True}],
                },
            )
            self.assertTrue(wave["ok"])
            self.assertTrue(wave["data"]["ran"])
            self.assertEqual(wave["data"]["execution"]["verified_changed"], 2)

            summary = app.call_tool("x_autopilot", {"action": "ops_summary"})
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["data"]["totals"]["completed"], 2)
            self.assertEqual(summary["data"]["follow_funnel"]["verified_follows"], 1)
            self.assertEqual(len(summary["data"]["autopilot"]["recent_waves"]), 1)

            stopped = app.call_tool("x_autopilot", {"action": "stop"})
            self.assertTrue(stopped["ok"])
            self.assertFalse(stopped["data"]["enabled"])


if __name__ == "__main__":
    unittest.main()
