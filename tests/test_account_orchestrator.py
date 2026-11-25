import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT / "src"), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from account_orchestrator import (  # noqa: E402
    ContentQueueStore,
    OrchestratorError,
    build_daily_orchestration_plan,
    evaluate_account_health,
    evaluate_circuit_breaker,
    find_unfollow_candidates,
    infer_owner_profile,
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
            "account_binding": "example_creator",
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
            "account_binding": "example_creator",
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
            "account_binding": "example_creator",
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
            "account_binding": "example_creator",
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
            account_snapshot={"display_name": "Example Creator", "bio": "Builder", "has_avatar": True, "posts_count": 12, "account_age_days": 14},
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
            with self.assertRaises(OrchestratorError) as dup:
                store.add(kind="original_post", text="shipped safe pacing today.")
            self.assertEqual(dup.exception.code, "DUPLICATE_CONTENT")
            item2 = store.add(kind="original_post", text="Later post.", scheduled_after_epoch=future)
            due = store.due()
            self.assertEqual([i["item_id"] for i in due], [item1["item_id"]])

            store.mark_published(item1["item_id"], published_url="https://x.com/example_creator/status/999")
            self.assertEqual(len(store.list(status="published")), 1)
            store.remove(item2["item_id"])
            self.assertEqual(len(store.list(status="queued")), 0)

    def test_daily_plan_deduplicates_and_interleaves(self) -> None:
        ledger = {
            "account_binding": "example_creator",
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
                {"url": "https://x.com/frank/status/104", "author": "frank", "text": "Benchmarking async lock contention and p99 latency in open-source MCP workers (+42% qps, 12ms p99)."},
            ],
            follow_candidates=[
                {"handle": "already_followed"},
                {"handle": "big_account", "followers_count": 50000, "following_count": 100},
                {"handle": "new_friend_1", "mutual_verified": True, "bio": "builder", "followers_count": 300, "following_count": 280},
                {"handle": "new_friend_2", "followers_count": 200, "following_count": 250},
            ],
            account_snapshot={"display_name": "Example Creator", "bio": "Builder", "has_avatar": True, "posts_count": 5, "account_age_days": 8},
            seed=42,
        )
        self.assertFalse(plan["blocked"])
        self.assertEqual(plan["status"], "planned_not_executed")
        # Day 8 with 5 posts is content_build stage (caps follow_profile at 5).
        self.assertEqual(plan["stage"], "content_build")
        self.assertEqual(plan["counts"], {"publish_post": 1, "like_post": 2, "follow_profile": 3, "repost_post": 1, "reply_post": 1})
        urls = [op.get("url") for op in plan["operations"] if op.get("url")]
        self.assertNotIn("https://x.com/bob/status/100", urls)
        handles = [op.get("handle") for op in plan["operations"] if op.get("handle")]
        self.assertNotIn("already_followed", handles)
        # Highest-scored candidate (new_friend_1) must come before big_account.
        self.assertLess(handles.index("new_friend_1"), handles.index("big_account"))

    def test_ghost_stage_and_ratio_guard_and_external_link_skip(self) -> None:
        ledger = {"account_binding": "example_creator", "actions": {}}
        # Ghost stage (0 active days): only likes <= 3, 0 follows, 0 publishes.
        ghost_plan = build_daily_orchestration_plan(
            ledger,
            goals={"publish_post": 2, "like_post": 10, "follow_profile": 5, "repost_post": 3},
            content_queue=[{"item_id": "cq_ext", "kind": "original_post", "status": "queued", "text": "Check https://example.com"}],
            timeline_posts=[
                {"url": "https://x.com/a/status/1", "author": "a", "text": "1"},
                {"url": "https://x.com/b/status/2", "author": "b", "text": "2"},
                {"url": "https://x.com/c/status/3", "author": "c", "text": "3"},
                {"url": "https://x.com/d/status/4", "author": "d", "text": "4"},
            ],
            follow_candidates=[{"handle": "cand1"}],
            account_snapshot={"posts_count": 0, "account_age_days": 1},
            seed=1,
        )
        self.assertEqual(ghost_plan["stage"], "ghost")
        self.assertEqual(ghost_plan["counts"]["like_post"], 3)
        self.assertEqual(ghost_plan["counts"]["follow_profile"], 0)
        self.assertEqual(ghost_plan["counts"]["publish_post"], 0)

        # Ratio guard + external link filter in content_build stage.
        ratio_plan = build_daily_orchestration_plan(
            ledger,
            goals={"publish_post": 2, "like_post": 2, "follow_profile": 5},
            content_queue=[
                {"item_id": "cq_ext", "kind": "original_post", "status": "queued", "text": "Link https://example.com"},
                {"item_id": "cq_clean", "kind": "original_post", "status": "queued", "text": "Pure text post"},
            ],
            follow_candidates=[{"handle": "cand1"}],
            account_snapshot={"posts_count": 5, "account_age_days": 8, "following_count": 200, "followers_count": 20},
            seed=1,
        )
        self.assertEqual(ratio_plan["counts"]["follow_profile"], 0)
        self.assertEqual(ratio_plan["counts"]["publish_post"], 1)
        self.assertTrue(ratio_plan["health"]["warmup"]["ratio_guard_triggered"])

        # Established X account (e.g., 866 posts) connecting with a fresh local ledger -> growth, not ghost.
        established = evaluate_account_health(
            ledger,
            account_snapshot={"display_name": "example_creator", "posts_count": 866},
        )
        self.assertEqual(established["warmup"]["stage"], "growth")


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
                state["account_binding"] = "example_creator"
                app._bridge()._write(state)

            # 1. Queue an original post via MCP.
            q = app.call_tool("x_content_queue", {"action": "add", "kind": "original_post", "text": "Automating X safely."})
            self.assertTrue(q["ok"])
            item_id = q["data"]["item"]["item_id"]

            # 2. Check health.
            h = app.call_tool("x_account_health", {})
            self.assertTrue(h["ok"])
            self.assertEqual(h["data"]["content_queue"]["due"], 1)

            # 3. Plan daily wave (provide growth-ready snapshot so publish_post is admitted).
            planned = app.call_tool(
                "x_daily_orchestrate_plan",
                {
                    "goals": {"publish_post": 1, "like_post": 1, "follow_profile": 1, "repost_post": 0},
                    "timeline_posts": [{"url": "https://x.com/alice/status/555", "author": "alice", "text": "hello"}],
                    "follow_candidates": [{"handle": "alice"}],
                    "account_snapshot": {"display_name": "Example Creator", "bio": "Builder", "has_avatar": True, "posts_count": 12, "account_age_days": 14},
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
                    "post_url": "https://x.com/example_creator/status/777",
                    "author": "example_creator",
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
                    "account_snapshot": {"display_name": "Example Creator", "bio": "Builder", "has_avatar": True, "posts_count": 12, "account_age_days": 14},
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

    def test_topic_relevance_filter_search_and_scheduler(self) -> None:
        ledger = {"account_binding": "example_creator", "actions": {}}
        plan = build_daily_orchestration_plan(
            ledger,
            goals={"publish_post": 0, "like_post": 2, "follow_profile": 0},
            timeline_posts=[
                {"url": "https://x.com/spam/status/1", "author": "spam", "text": "Free crypto airdrop giveaway!"},
                {"url": "https://x.com/other/status/2", "author": "other", "text": "Good morning everyone"},
                {"url": "https://x.com/ai/status/3", "author": "ai", "text": "Building an AI Agent with MCP"},
            ],
            account_snapshot={"posts_count": 75},
            persona_topics=["AI", "MCP"],
            exclude_keywords=["airdrop", "giveaway"],
            seed=1,
        )
        urls = [op["url"] for op in plan["operations"]]
        self.assertNotIn("https://x.com/spam/status/1", urls)
        # Topic-matching post must be ranked first.
        self.assertEqual(urls[0], "https://x.com/ai/status/3")

        with tempfile.TemporaryDirectory() as tmp:
            app = XStarter(
                mode="browser",
                environ={
                    "X_STARTER_MODE": "browser",
                    "X_STARTER_ENABLE_BROWSER": "1",
                    "X_STARTER_STATE_DIR": tmp,
                },
            )
            try:
                mock_browser = MagicMock()
                mock_browser.open.return_value = {
                    "url": "https://x.com/search?q=MCP&f=live",
                    "text": "MCP post",
                    "articles": [{"text": "MCP search hit", "links": ["https://x.com/dev/status/901"]}],
                }
                app._browser_for = lambda _args: mock_browser
                srch = app.call_tool("x_search_posts", {"query": "MCP", "limit": 5})
                self.assertTrue(srch["ok"])
                self.assertEqual(srch["data"]["posts"][0]["url"], "https://x.com/dev/status/901")

                # Generic AI cliché replies must be rejected before entering the queue.
                bad_reply = app.call_tool(
                    "x_content_queue",
                    {
                        "action": "add",
                        "kind": "reply",
                        "target_url": "https://x.com/dev/status/901",
                        "text": "Great post! Very insightful, thanks for sharing!",
                    },
                )
                self.assertFalse(bad_reply["ok"])
                self.assertEqual(bad_reply["error"]["code"], "REPLY_TOO_GENERIC")

                # Visiting a profile reads their recent posts.
                mock_browser.open.return_value = {
                    "url": "https://x.com/dev",
                    "text": "dev posts",
                    "articles": [{"text": "Concrete benchmark on MCP latency", "links": ["https://x.com/dev/status/902"]}],
                }
                visit = app.call_tool("x_visit_profile", {"username": "dev", "limit": 5})
                self.assertTrue(visit["ok"])
                self.assertEqual(visit["data"]["posts"][0]["url"], "https://x.com/dev/status/902")

                st = app.call_tool("x_autopilot", {"action": "scheduler_start"})
                self.assertTrue(st["ok"])
                self.assertTrue(st["data"]["scheduler"]["running"])
                sp = app.call_tool("x_autopilot", {"action": "scheduler_stop"})
                self.assertTrue(sp["ok"])
                self.assertFalse(sp["data"]["scheduler"]["running"])

                inferred = infer_owner_profile(
                    [
                        {"text": "Building an AI Agent with MCP today"},
                        {"text": "MCP tool schemas and Agent orchestration"},
                    ],
                    username="example_creator",
                )
                self.assertIn("Agent", inferred["persona_topics"])
                self.assertIn("MCP", inferred["persona_topics"])
                self.assertTrue(inferred["search_query"])

                # Reciprocal wave & 7-day non-reciprocal unfollow cleanup.
                mock_browser.browser_family = "chromium"
                mock_browser.execute_x_action.side_effect = lambda *, action_type, post_url, author, text, action_id, claim_token, lease_expires_epoch: (
                    {"handle": author, "profile_url": post_url, "state": "unfollowed", "verified": True, "changed": True, "mutual_verified": False}
                    if action_type == "unfollow_profile"
                    else (
                        {"handle": author, "profile_url": post_url, "state": "following", "verified": True, "changed": True}
                        if action_type == "follow_profile"
                        else {"post_url": post_url, "author": author, "state": "liked", "verified": True, "changed": True}
                    )
                )
                rec = app.call_tool(
                    "x_autopilot",
                    {
                        "action": "reciprocal_wave",
                        "max_count": 2,
                        "respect_pacing": False,
                        "timeline_posts": [
                            {"url": "https://x.com/huguan_dev/status/910", "author": "huguan_dev", "text": "互关 AI Agent 一起交流"},
                        ],
                        "account_snapshot": {"username": "example_creator", "posts_count": 75},
                    },
                )
                self.assertTrue(rec["ok"])
                self.assertTrue(rec["data"]["ran"])

                old_ledger = {
                    "account_binding": "example_creator",
                    "actions": {
                        "f_old": _action(
                            "f_old",
                            "follow_profile",
                            completed_at="2026-11-01T10:00:00+08:00",
                            payload={"handle": "ghost_user", "profile_url": "https://x.com/ghost_user"},
                            result={"handle": "ghost_user", "profile_url": "https://x.com/ghost_user", "state": "following", "verified": True, "changed": True, "mutual_verified": False},
                        ),
                        "f_mut": _action(
                            "f_mut",
                            "follow_profile",
                            completed_at="2026-11-01T10:00:00+08:00",
                            payload={"handle": "friend_user", "profile_url": "https://x.com/friend_user"},
                            result={"handle": "friend_user", "profile_url": "https://x.com/friend_user", "state": "following", "verified": True, "changed": True, "mutual_verified": True},
                        ),
                    },
                }
                cands = find_unfollow_candidates(old_ledger, min_age_days=7, now_epoch=1800000000.0)
                self.assertEqual([c["handle"] for c in cands], ["ghost_user"])

                with app._bridge()._lock:
                    st_data = app._bridge()._read()
                    st_data["actions"]["f_old"] = old_ledger["actions"]["f_old"]
                    app._bridge()._write(st_data)

                cln = app.call_tool("x_autopilot", {"action": "run_cleanup", "min_age_days": 7, "max_count": 2, "respect_pacing": False})
                self.assertTrue(cln["ok"])
                self.assertTrue(cln["data"]["ran"])
                self.assertEqual(cln["data"]["execution"]["verified_changed"], 1)

                # 100/day reciprocal marker filter & x_notifications_read.
                rec_100 = build_daily_orchestration_plan(
                    {"account_binding": "example_creator", "actions": {}},
                    goals={"follow_profile": 100, "like_post": 0, "publish_post": 0},
                    follow_candidates=[
                        {"handle": "normal_user", "bio": "software engineer"},
                        {"handle": "huguan_100", "bio": "互关 互follow"},
                    ],
                    account_snapshot={"posts_count": 75},
                    require_reciprocal_marker=True,
                )
                self.assertEqual([op["handle"] for op in rec_100["operations"]], ["huguan_100"])

                mock_browser.open.return_value = {
                    "url": "https://x.com/notifications",
                    "text": "notifications",
                    "articles": [{"text": "alice replied: nice MCP tool!", "links": ["https://x.com/alice/status/999"]}],
                }
                notif = app.call_tool("x_notifications_read", {"limit": 10})
                self.assertTrue(notif["ok"])
                self.assertEqual(notif["data"]["interactions"][0]["author"], "alice")
            finally:
                app.close()

    def test_persona_memory_persists_and_blocks_custom_banned_phrases(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = XStarter(
                mode="browser",
                environ={
                    "X_STARTER_MODE": "browser",
                    "X_STARTER_ENABLE_BROWSER": "1",
                    "X_STARTER_STATE_DIR": tmpdir,
                },
            )
            try:
                got = app.call_tool("x_persona_memory", {"action": "get"})
                self.assertTrue(got["ok"])
                self.assertEqual(got["data"]["role"], "")

                updated = app.call_tool(
                    "x_persona_memory",
                    {
                        "action": "update",
                        "role": "Indie dev building MCP tooling",
                        "voice_rules": ["short sentences", "concrete metrics"],
                        "content_pillars": ["mcp", "browser-automation"],
                        "banned_phrases": ["synergy", "game-changer"],
                    },
                )
                self.assertTrue(updated["ok"])
                self.assertEqual(updated["data"]["role"], "Indie dev building MCP tooling")
                self.assertEqual(updated["data"]["banned_phrases"], ["synergy", "game-changer"])

                ep = app.call_tool(
                    "x_persona_memory",
                    {
                        "action": "add_episode",
                        "topic": "MCP schema clamp",
                        "angle": "why 2^53-1 matters",
                        "outcome": "12 replies",
                    },
                )
                self.assertTrue(ep["ok"])
                self.assertEqual(len(ep["data"]["episodic_notes"]), 1)

                fb = app.call_tool(
                    "x_persona_memory",
                    {
                        "action": "add_feedback",
                        "note": "Avoid hype adjectives",
                    },
                )
                self.assertTrue(fb["ok"])
                self.assertEqual(fb["data"]["feedback_notes"][0]["note"], "Avoid hype adjectives")

                blocked = app.call_tool(
                    "x_content_queue",
                    {
                        "action": "add",
                        "text": "This new MCP bridge is a true game-changer for our workflow.",
                    },
                )
                self.assertFalse(blocked["ok"])
                self.assertEqual(blocked["error"]["code"], "BANNED_PHRASE")

                allowed = app.call_tool(
                    "x_content_queue",
                    {
                        "action": "add",
                        "text": "Clamped integer bounds to 2^53-1 in the MCP bridge today.",
                    },
                )
                self.assertTrue(allowed["ok"])
            finally:
                app.close()

    def test_pillar_weights_and_thread_composer_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = XStarter(
                mode="browser",
                environ={
                    "X_STARTER_MODE": "browser",
                    "X_STARTER_ENABLE_BROWSER": "1",
                    "X_STARTER_STATE_DIR": tmpdir,
                },
            )
            try:
                with app._bridge()._lock:
                    st = app._bridge()._read()
                    st["account_binding"] = "example_creator"
                    app._bridge()._write(st)
                app.call_tool(
                    "x_persona_memory",
                    {
                        "action": "add_episode",
                        "topic": "mcp",
                        "angle": "schema bounds",
                        "outcome": "25 likes, 5 replies",
                    },
                )
                app.call_tool(
                    "x_persona_memory",
                    {
                        "action": "add_episode",
                        "topic": "coffee",
                        "angle": "morning cup",
                        "outcome": "1 like",
                    },
                )
                pw = app.call_tool("x_persona_memory", {"action": "pillar_weights"})
                self.assertTrue(pw["ok"])
                self.assertEqual(pw["data"]["recommended_top"][0], "mcp")
                self.assertGreater(
                    pw["data"]["weights_map"]["mcp"],
                    pw["data"]["weights_map"]["coffee"],
                )

                # Queue lower-weighted 'coffee' first, higher-weighted 'mcp' second -> planner picks 'mcp' first.
                p_coffee = app.call_tool("x_content_queue", {"action": "add", "kind": "original_post", "topic": "coffee", "text": "Morning pour-over before debugging."})
                p_mcp = app.call_tool("x_content_queue", {"action": "add", "kind": "original_post", "topic": "mcp", "text": "Why bounded MCP integer schemas prevent upstream 500s."})
                weighted_plan = app.call_tool(
                    "x_daily_orchestrate_plan",
                    {
                        "goals": {"publish_post": 1, "like_post": 0, "follow_profile": 0, "repost_post": 0, "reply_post": 0},
                        "account_snapshot": {"posts_count": 50},
                    },
                )
                self.assertTrue(weighted_plan["ok"])
                self.assertEqual(weighted_plan["data"]["operations"][0]["topic"], "mcp")

                app.call_tool("x_content_queue", {"action": "remove", "item_id": p_coffee["data"]["item"]["item_id"]})
                app.call_tool("x_content_queue", {"action": "remove", "item_id": p_mcp["data"]["item"]["item_id"]})

                # Queue a 3-tweet thread under 'mcp' and run plan -> prepare -> execute chaining URLs.
                thr = app.call_tool(
                    "x_content_queue",
                    {
                        "action": "add",
                        "kind": "thread",
                        "topic": "mcp",
                        "tweets": [
                            "1/ How we kept X MCP automation safe under strict browser pacing.",
                            "2/ Every write goes through prepare -> confirmation -> leased claim.",
                            "3/ Follow-ups chain dynamically onto the published status URL.",
                        ],
                    },
                )
                self.assertTrue(thr["ok"])
                self.assertEqual(len(thr["data"]["item"]["tweets"]), 3)

                plan = app.call_tool(
                    "x_daily_orchestrate_plan",
                    {
                        "goals": {"publish_post": 1, "reply_post": 2, "like_post": 0, "follow_profile": 0, "repost_post": 0},
                        "account_snapshot": {"posts_count": 50},
                    },
                )
                self.assertTrue(plan["ok"])
                ops = plan["data"]["operations"]
                self.assertEqual([op["action"] for op in ops[:3]], ["publish_post", "reply_post", "reply_post"])
                self.assertTrue(ops[1]["reply_to_previous_thread_step"])

                prep = app.call_tool("x_daily_orchestrate_prepare", {"plan": plan["data"]})
                self.assertTrue(prep["ok"])

                import hashlib
                mock_browser = MagicMock()
                mock_browser.browser_family = "chromium"
                mock_browser.execute_x_publish.side_effect = lambda *, text, expected_username, action_id, claim_token, lease_expires_epoch: {
                    "verified": True,
                    "changed": True,
                    "state": "published",
                    "author": "example_creator",
                    "text": text,
                    "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "post_url": "https://x.com/example_creator/status/1001",
                }
                mock_browser.execute_x_action.side_effect = [
                    {
                        "verified": True,
                        "changed": True,
                        "state": "replied",
                        "author": "example_creator",
                        "post_url": "https://x.com/example_creator/status/1001",
                        "reply_url": "https://x.com/example_creator/status/1002",
                        "text": "2/ Every write goes through prepare -> confirmation -> leased claim.",
                    },
                    {
                        "verified": True,
                        "changed": True,
                        "state": "replied",
                        "author": "example_creator",
                        "post_url": "https://x.com/example_creator/status/1002",
                        "reply_url": "https://x.com/example_creator/status/1003",
                        "text": "3/ Follow-ups chain dynamically onto the published status URL.",
                    },
                ]
                with patch.object(app, "_browser_for", return_value=mock_browser):
                    exe = app.call_tool(
                        "x_daily_orchestrate_execute",
                        {
                            "batch_id": prep["data"]["batch_id"],
                            "confirmation": prep["data"]["confirmation_phrase"],
                            "respect_pacing": False,
                        },
                    )
                self.assertTrue(exe["ok"])
                self.assertEqual(exe["data"]["verified_changed"], 3)
                self.assertEqual(mock_browser.execute_x_action.call_args_list[0].kwargs["post_url"], "https://x.com/example_creator/status/1001")
                self.assertEqual(mock_browser.execute_x_action.call_args_list[1].kwargs["post_url"], "https://x.com/example_creator/status/1002")
            finally:
                app.close()

    def test_cross_language_discovery_query_rotation_and_scoring(self) -> None:
        from account_orchestrator import pick_discovery_query, score_follow_candidate

        q0 = pick_discovery_query({}, wave_index=0)
        q1 = pick_discovery_query({}, wave_index=1)
        q3 = pick_discovery_query({}, wave_index=3)
        q4 = pick_discovery_query({}, wave_index=4)
        self.assertNotEqual(q0, q1)
        self.assertIn("build in public", q0)
        self.assertIn("相互フォロー", q3)
        self.assertIn("개발자", q4)

        en_score, _ = score_follow_candidate({"handle": "en_builder", "bio": "Indie hacker, build in public, follow back", "active": True})
        ja_score, _ = score_follow_candidate({"handle": "ja_dev", "bio": "個人開発 相互フォロー", "active": True})
        zombie_score, _ = score_follow_candidate({"handle": "zombie_farm", "bio": "airdrop giveaway", "followers_count": 2, "following_count": 1200})
        self.assertGreater(en_score, zombie_score)
        self.assertGreater(ja_score, zombie_score)
        self.assertLess(zombie_score, 0)

    def test_ops_summary_and_export_report_persist_local_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = XStarter(
                mode="browser",
                environ={
                    "X_STARTER_MODE": "browser",
                    "X_STARTER_ENABLE_BROWSER": "1",
                    "X_STARTER_STATE_DIR": tmpdir,
                },
            )
            try:
                summary = app.call_tool("x_autopilot", {"action": "ops_summary"})
                self.assertTrue(summary["ok"])
                local_files = summary["data"]["local_files"]
                self.assertTrue(Path(local_files["history_json"]).exists())
                self.assertTrue(Path(local_files["metrics_csv"]).exists())
                self.assertGreaterEqual(local_files["days_recorded"], 7)

                exported = app.call_tool("x_autopilot", {"action": "export_report"})
                self.assertTrue(exported["ok"])
                self.assertIn("date,publish_post,like_post,follow_profile", exported["data"]["csv_content"])
                self.assertIn("persona_memory", exported["data"])
                self.assertIn("content_queue", exported["data"])
            finally:
                app.close()

    def test_target_pool_ingest_and_zero_search_wave(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = XStarter(
                mode="browser",
                environ={
                    "X_STARTER_MODE": "browser",
                    "X_STARTER_ENABLE_BROWSER": "1",
                    "X_STARTER_STATE_DIR": tmpdir,
                },
            )
            try:
                ing = app.call_tool(
                    "x_target_pool",
                    {
                        "action": "ingest",
                        "source": "scout_alt",
                        "candidates": [
                            {"handle": "scout_found_1", "bio": "build in public", "active": True},
                        ],
                        "posts": [
                            {"url": "https://x.com/scout_found_1/status/888001", "author": "scout_found_1", "text": "Shipping MCP today"},
                        ],
                    },
                )
                self.assertTrue(ing["ok"])
                self.assertEqual(ing["data"]["added_candidates"], 1)
                self.assertEqual(ing["data"]["added_posts"], 1)
                self.assertEqual(ing["data"]["fresh_candidates"], 1)

                prep = app.call_tool("x_autopilot", {"action": "prepare", "days": 7, "max_per_wave": 4})
                app.call_tool(
                    "x_autopilot",
                    {
                        "action": "activate",
                        "window_id": prep["data"]["window_id"],
                        "confirmation": prep["data"]["confirmation_phrase"],
                    },
                )
                mock_browser = MagicMock()
                mock_browser.browser_family = "chromium"
                mock_browser.execute_x_action.side_effect = lambda *, action_type, post_url, author, text, action_id, claim_token, lease_expires_epoch: (
                    {"verified": True, "changed": True, "state": "liked", "post_url": post_url}
                    if action_type == "like_post"
                    else (
                        {"verified": True, "changed": True, "state": "replied", "post_url": post_url, "reply_url": "https://x.com/example_creator/status/999001", "text": text}
                        if action_type == "reply_post"
                        else {"verified": True, "changed": True, "state": "following", "handle": author, "profile_url": post_url}
                    )
                )
                with patch.object(app, "_browser_for", return_value=mock_browser):
                    wave = app.call_tool(
                        "x_autopilot",
                        {
                            "action": "run_wave",
                            "force": True,
                            "respect_pacing": False,
                            "account_snapshot": {"posts_count": 75},
                        },
                    )
                self.assertTrue(wave["ok"])
                self.assertTrue(wave["data"]["ran"])
                self.assertGreaterEqual(wave["data"]["execution"]["verified_changed"], 2)
                st = app.call_tool("x_target_pool", {"action": "stats"})
                self.assertEqual(st["data"]["fresh_candidates"], 0)
                self.assertEqual(st["data"]["fresh_posts"], 0)
            finally:
                app.close()

    def test_auto_reply_drafts_multilingual_friend_making_replies(self) -> None:
        from account_orchestrator import check_reply_naturalness

        plan = build_daily_orchestration_plan(
            {"account_binding": "example_creator", "actions": {}},
            goals={"publish_post": 0, "like_post": 2, "reply_post": 2, "follow_profile": 0, "repost_post": 0},
            timeline_posts=[
                {"url": "https://x.com/alice_zh/status/301", "author": "alice_zh", "text": "今天在用 MCP 搭一个自动 Agent 工具链"},
                {"url": "https://x.com/bob_en/status/302", "author": "bob_en", "text": "Shipping an indie AI agent in public this week"},
            ],
            account_snapshot={"posts_count": 75},
            auto_reply=True,
            persona_memory={"banned_phrases": ["synergy"]},
            seed=7,
        )
        replies = [op for op in plan["operations"] if op["action"] == "reply_post"]
        self.assertEqual(len(replies), 2)
        for rep in replies:
            self.assertTrue(rep.get("auto_drafted"))
            self.assertEqual(check_reply_naturalness(rep["text"], extra_banned=["synergy"]), [])

    def test_persist_daily_metrics_skips_unchanged_rewrites(self) -> None:
        from autopilot_ops import persist_daily_metrics

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            summary = {
                "timezone": "Asia/Shanghai",
                "follow_funnel": {"verified_follows": 4, "mutual_verified": 1, "follow_back_rate": 0.25},
                "uncertain_rate": 0.0,
                "daily_breakdown": [
                    {"date": "2026-11-22", "publish_post": 0, "like_post": 2, "follow_profile": 4, "repost_post": 0, "reply_post": 1},
                ],
            }
            first = persist_daily_metrics(root, summary)
            self.assertEqual(first["days_recorded"], 1)
            history_path = Path(first["history_json"])
            csv_path = Path(first["metrics_csv"])
            sentinel_ns = history_path.stat().st_mtime_ns - 5_000_000_000
            os.utime(history_path, ns=(sentinel_ns, sentinel_ns))
            os.utime(csv_path, ns=(sentinel_ns, sentinel_ns))

            second = persist_daily_metrics(root, summary)
            self.assertEqual(second["csv_content"], first["csv_content"])
            self.assertEqual(history_path.stat().st_mtime_ns, sentinel_ns)
            self.assertEqual(csv_path.stat().st_mtime_ns, sentinel_ns)

            updated = {
                **summary,
                "daily_breakdown": [
                    {"date": "2026-11-22", "publish_post": 0, "like_post": 3, "follow_profile": 5, "repost_post": 0, "reply_post": 1},
                ],
            }
            third = persist_daily_metrics(root, updated)
            self.assertIn(",3,5,", third["csv_content"])
            self.assertGreater(history_path.stat().st_mtime_ns, sentinel_ns)
            self.assertFalse(list(root.glob("*.tmp")))

    def test_call_tool_maps_lock_timeout_to_structured_busy_error(self) -> None:
        from ledger_lock import LedgerLock

        with tempfile.TemporaryDirectory() as tmpdir:
            app = XStarter(
                mode="browser",
                environ={
                    "X_STARTER_MODE": "browser",
                    "X_STARTER_ENABLE_BROWSER": "1",
                    "X_STARTER_STATE_DIR": tmpdir,
                },
            )
            try:
                store = app._autopilot_store()
                orig_timeout = store.lock.timeout_seconds
                blocker = LedgerLock(store.lock.path, timeout_seconds=1)
                blocker.__enter__()
                store.lock.timeout_seconds = 0
                try:
                    res = app.call_tool("x_autopilot", {"action": "status"})
                finally:
                    store.lock.timeout_seconds = orig_timeout
                    blocker.__exit__()
                self.assertFalse(res["ok"])
                self.assertEqual(res["error"]["code"], "BRIDGE_BUSY")
                self.assertTrue(res["error"]["retryable"])
            finally:
                app.close()

    def test_200_follow_wave_slice_prioritizes_follows_within_deadline(self) -> None:
        ledger = {"account_binding": "example_creator", "actions": {}}
        cands = [{"handle": f"builder_{i}", "bio": "indie hacker", "active": True} for i in range(25)]
        posts = [{"url": f"https://x.com/poster_{i}/status/{1000 + i}", "author": f"poster_{i}", "text": f"Shipping MCP day {i}"} for i in range(15)]
        plan = build_daily_orchestration_plan(
            ledger,
            goals={"publish_post": 0, "like_post": 45, "follow_profile": 200, "repost_post": 6, "reply_post": 12},
            timeline_posts=posts,
            follow_candidates=cands,
            account_snapshot={"posts_count": 77},
            auto_reply=True,
            seed=11,
        )
        wave = plan["operations"][:12]
        follow_count = sum(1 for op in wave if op["action"] == "follow_profile")
        self.assertGreaterEqual(follow_count, 8)
        total_delay = sum(op["delay_before_seconds"] for op in wave)
        self.assertLess(total_delay, 900)
        for op in wave[1:]:
            self.assertGreaterEqual(op["delay_before_seconds"], 25)

        with tempfile.TemporaryDirectory() as tmpdir:
            app = XStarter(
                mode="browser",
                environ={
                    "X_STARTER_MODE": "browser",
                    "X_STARTER_ENABLE_BROWSER": "1",
                    "X_STARTER_STATE_DIR": tmpdir,
                },
            )
            try:
                prep = app.call_tool(
                    "x_autopilot",
                    {
                        "action": "prepare",
                        "days": 7,
                        "max_per_wave": 12,
                        "min_wave_interval_minutes": 12,
                        "daily_goals": {"publish_post": 1, "like_post": 45, "follow_profile": 200, "repost_post": 6, "reply_post": 12},
                    },
                )
                self.assertTrue(prep["ok"])
                self.assertEqual(prep["data"]["max_per_wave"], 12)
                self.assertEqual(prep["data"]["min_wave_interval_minutes"], 12)
                st = app.call_tool("x_autopilot", {"action": "status"})
                self.assertEqual(len(st["data"]["pending_prepared"]), 1)
            finally:
                app.close()

    def test_run_wave_continues_past_single_uncertain_but_stops_on_rate_limit_or_streak(self) -> None:
        from browser_executor import ExecutorError

        with tempfile.TemporaryDirectory() as tmpdir:
            app = XStarter(
                mode="browser",
                environ={
                    "X_STARTER_MODE": "browser",
                    "X_STARTER_ENABLE_BROWSER": "1",
                    "X_STARTER_STATE_DIR": tmpdir,
                },
            )
            try:
                prep = app.call_tool("x_autopilot", {"action": "prepare", "days": 7, "max_per_wave": 4})
                app.call_tool(
                    "x_autopilot",
                    {"action": "activate", "window_id": prep["data"]["window_id"], "confirmation": prep["data"]["confirmation_phrase"]},
                )
                calls: list[str] = []

                def fake_action(*, action_type, post_url, author, text, action_id, claim_token, lease_expires_epoch):
                    calls.append(author)
                    if author == "flaky_first":
                        raise ExecutorError("ACTION_UNCERTAIN", "ambiguous profile")
                    return {"verified": True, "changed": True, "state": "following", "handle": author, "profile_url": post_url}

                mock_browser = MagicMock()
                mock_browser.browser_family = "chromium"
                mock_browser.execute_x_action.side_effect = fake_action
                with patch.object(app, "_browser_for", return_value=mock_browser):
                    wave = app.call_tool(
                        "x_autopilot",
                        {
                            "action": "run_wave",
                            "force": True,
                            "respect_pacing": False,
                            "timeline_posts": [],
                            "follow_candidates": [
                                {"handle": "flaky_first", "mutual_verified": True},
                                {"handle": "good_second", "bio": "builder"},
                                {"handle": "good_third", "bio": "builder"},
                            ],
                            "account_snapshot": {"posts_count": 77},
                        },
                    )
                self.assertTrue(wave["ok"])
                self.assertEqual(wave["data"]["execution"]["processed"], 3)
                self.assertEqual(wave["data"]["execution"]["verified_changed"], 2)
                self.assertEqual(set(calls), {"flaky_first", "good_second", "good_third"})
                self.assertIsNone(wave["data"]["execution"]["stop_reason"])

                def rate_limited(*, action_type, post_url, author, text, action_id, claim_token, lease_expires_epoch):
                    raise ExecutorError("X_RATE_LIMITED", "HTTP 429", details={"retry_after_seconds": 60})

                mock_browser.execute_x_action.side_effect = rate_limited
                with patch.object(app, "_browser_for", return_value=mock_browser):
                    rl_wave = app.call_tool(
                        "x_autopilot",
                        {
                            "action": "run_wave",
                            "force": True,
                            "respect_pacing": False,
                            "timeline_posts": [],
                            "follow_candidates": [{"handle": "next_one"}, {"handle": "next_two"}],
                            "account_snapshot": {"posts_count": 77},
                        },
                    )
                self.assertTrue(rl_wave["ok"])
                self.assertEqual(rl_wave["data"]["execution"]["stop_reason"], "X_RATE_LIMITED")
                self.assertEqual(rl_wave["data"]["execution"]["processed"], 1)
            finally:
                app.close()

    def test_autonomous_scheduler_executes_wave_and_releases_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = XStarter(
                mode="browser",
                environ={
                    "X_STARTER_MODE": "browser",
                    "X_STARTER_ENABLE_BROWSER": "1",
                    "X_STARTER_STATE_DIR": tmpdir,
                },
            )
            try:
                app._owner_profile = Path(tmpdir) / "owner-prof"
                app._owner_profile.mkdir()
                with app._bridge()._lock:
                    st_data = app._bridge()._read()
                    st_data["actions"]["obs_init"] = _action(
                        "obs_init", "observe_account", result={"username": "example_creator", "posts_count": 77, "verified": True}
                    )
                    app._bridge()._write(st_data)
                prep = app.call_tool("x_autopilot", {"action": "prepare", "days": 7, "active_hours": [0, 24], "max_per_wave": 2})
                app.call_tool(
                    "x_autopilot",
                    {"action": "activate", "window_id": prep["data"]["window_id"], "confirmation": prep["data"]["confirmation_phrase"]},
                )
                app.call_tool(
                    "x_target_pool",
                    {"action": "ingest", "candidates": [{"handle": "sched_target_1", "active": True}, {"handle": "sched_target_2", "active": True}]},
                )
                mock_browser = MagicMock()
                mock_browser.browser_family = "chromium"
                mock_browser.execute_x_action.side_effect = lambda *, action_type, post_url, author, text, action_id, claim_token, lease_expires_epoch: {
                    "verified": True, "changed": True, "state": "following", "handle": author, "profile_url": post_url
                }
                orig_browser_for = app._browser_for

                def tracked_browser_for(arguments):
                    app._owner_session = arguments.get("_mcp_session_id")
                    app._browsers[app._owner_session] = mock_browser
                    return mock_browser

                with patch.object(app, "_browser_for", side_effect=tracked_browser_for):
                    import threading as _th
                    _waiter = _th.Event()
                    with patch("server.time.sleep"):
                        app._ensure_scheduler_started()
                        assert app._scheduler is not None
                        app._scheduler._poll_seconds = 5.0
                        deadline = time.monotonic() + 5.0
                        while time.monotonic() < deadline:
                            st = app._scheduler.status()
                            if any("ran wave" in str(item.get("outcome") or "") for item in (st.get("tick_log") or [])):
                                break
                            _waiter.wait(0.05)
                        app._scheduler.stop()
                    outcomes = [str(item.get("outcome") or "") for item in (app._scheduler.status().get("tick_log") or [])]
                    self.assertTrue(any("ran wave (2/2 verified)" in o for o in outcomes), outcomes)
                    self.assertIsNone(app._owner_session)
                    self.assertEqual(app._browsers, {})
                summary = app.call_tool("x_autopilot", {"action": "ops_summary"})
                self.assertEqual(summary["data"]["follow_funnel"]["verified_follows"], 2)
                self.assertEqual(len(summary["data"]["autopilot"]["recent_waves"]), 1)
            finally:
                app.close()

    def test_tech_casual_persona_template_sync_and_cautious_reply_guards(self) -> None:
        from account_orchestrator import draft_friendly_reply

        with tempfile.TemporaryDirectory() as tmpdir:
            app = XStarter(
                mode="browser",
                environ={
                    "X_STARTER_MODE": "browser",
                    "X_STARTER_ENABLE_BROWSER": "1",
                    "X_STARTER_STATE_DIR": tmpdir,
                },
            )
            try:
                upd = app.call_tool("x_persona_memory", {"action": "update", "preset": "tech_casual"})
                self.assertTrue(upd["ok"])
                self.assertIn("写代码", upd["data"]["role"])
                self.assertIn("Caught this on my feed", upd["data"]["banned_phrases"])

                synced = app.call_tool(
                    "x_persona_memory",
                    {
                        "action": "sync_owner",
                        "account_snapshot": {"username": "example_creator", "display_name": "Example Creator", "bio": "Systems engineer building Rust and MCP agents"},
                        "own_posts": [
                            {"text": "Shipping MCP lock fixes and Playwright verification guardrails", "created_at": "2026-11-22T09:00:00Z"},
                            {"text": "Why reply pacing needs conservative backoff on X", "created_at": "2026-11-22T12:00:00Z"},
                            {"text": "Engineering rigor beats blind growth hacks", "created_at": "2026-11-22T15:00:00Z"},
                        ],
                    },
                )
                self.assertTrue(synced["ok"])
                self.assertTrue(synced["data"]["synced"])
                self.assertIn("写代码", synced["data"]["persona"]["role"])
                self.assertTrue(synced["data"]["changes"])
                self.assertEqual(synced["data"]["persona"]["owner_sync"]["owner_bio"], "Systems engineer building Rust and MCP agents")

                throttled = app.call_tool("x_persona_memory", {"action": "sync_owner"})
                self.assertTrue(throttled["ok"])
                self.assertFalse(throttled["data"]["synced"])
                self.assertEqual(throttled["data"]["reason"], "RECENTLY_SYNCED")

                first_reply = draft_friendly_reply(
                    {"text": "Working on MCP agent workflows and Playwright reliability today."},
                    persona_memory=synced["data"]["persona"],
                )
                self.assertIsNotNone(first_reply)
                second_reply = draft_friendly_reply(
                    {"text": "Working on MCP agent workflows and Playwright reliability today."},
                    persona_memory=synced["data"]["persona"],
                    recent_replies=[first_reply],
                )
                self.assertIsNotNone(second_reply)
                self.assertNotEqual(first_reply, second_reply)

                from datetime import datetime, timezone
                sec_ts = "2026-11-22T10:00:00+00:00"
                sec_epoch = datetime.fromisoformat(sec_ts).timestamp()
                sec_ledger = {
                    "account_binding": "example_creator",
                    "actions": {
                        "sec_1": {
                            "action_id": "sec_1",
                            "type": "reply_post",
                            "status": "uncertain",
                            "completed_at": sec_ts,
                            "error": {"code": "X_SECURITY_INTERSTITIAL"},
                        }
                    },
                }
                cb_block = evaluate_circuit_breaker(sec_ledger, now_epoch=sec_epoch + 1800.0)
                self.assertFalse(cb_block["allow_write"])
                self.assertEqual(cb_block["reason"], "SECURITY_INTERSTITIAL_COOLDOWN")
            finally:
                app.close()

    def test_contextual_multilang_reply_drafting_avoids_exhaustion(self) -> None:
        from account_orchestrator import draft_friendly_reply

        posts = [
            {"author": "dev_1", "text": "Debugging an async lock contention issue on Rust tokio workers."},
            {"author": "dev_2", "text": "New benchmark results for MCP latency under agent bursts."},
            {"author": "dev_3", "text": "Why Playwright DOM selectors beat screenshot OCR on dynamic feeds."},
            {"author": "dev_4", "text": "Guardrails and circuit breakers saved our scheduler during 429 spikes."},
            {"author": "dev_5", "text": "今天把 MCP 调度器的锁和限流防线重构成最小状态机了，延迟稳定很多。"},
            {"author": "dev_6", "text": "做系统架构的时候最怕半夜回调失败静默丢任务。"},
            {"author": "dev_7", "text": "Playwright のDOM検証でブラウザ自動化がかなり安定した。"},
            {"author": "dev_8", "text": "エージェントの安全ガードとレート制御を設計中です。"},
            {"author": "dev_9", "text": "MCP 에이전트 워크플로우 자동화 테스트가 깔끔하게 통과했네요."},
            {"author": "dev_10", "text": "Shipping a small SQLite WAL cache tweak for local agent runs."},
        ]
        recent: list[str] = []
        persona = {"tone": "concise_friendly", "role": "独立开发者"}
        for post in posts:
            reply = draft_friendly_reply(post, persona_memory=persona, recent_replies=recent)
            self.assertIsNotNone(reply, f"Exhausted reply bank for {post}")
            assert reply is not None
            self.assertGreater(len(reply.strip()), 6)
            self.assertNotIn(reply, recent)
            recent.append(reply)
        self.assertEqual(len(set(recent)), len(posts))
        # Ensure technical anchor words appear in English technical replies rather than pure generic filler
        anchors = ("rust", "tokio", "lock", "latency", "benchmark", "mcp", "playwright", "dom", "guardrail", "429")
        self.assertTrue(any(any(k in r.lower() for k in anchors) for r in recent[:4]), recent[:4])
        # Ensure Chinese replies stay Chinese and Korean/Japanese stay localized
        self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in recent[4]), recent[4])
        self.assertTrue(any("\u3040" <= ch <= "\u30ff" for ch in recent[6]), recent[6])
        self.assertTrue(any("\uac00" <= ch <= "\ud7af" for ch in recent[8]), recent[8])

    def test_mutual_follower_ledger_reconciliation_and_evidence_integrity(self) -> None:
        from datetime import datetime, timezone
        from autopilot_ops import build_ops_summary
        from browser_bridge import BrowserBridge
        import browser_read

        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = BrowserBridge(Path(tmpdir), account_binding="example_creator")
            old_ts = datetime.fromtimestamp(time.time() - 5 * 86400, tz=timezone.utc).isoformat()
            with bridge._lock:
                data = bridge._read()
                data["account_binding"] = "example_creator"
                data["actions"]["act_f1"] = _action(
                    "act_f1",
                    "follow_profile",
                    completed_at=old_ts,
                    payload={"handle": "Alice_Builder"},
                    result={"verified": True, "changed": True, "state": "following", "handle": "Alice_Builder"},
                )
                data["actions"]["act_f2"] = _action(
                    "act_f2",
                    "follow_profile",
                    completed_at=old_ts,
                    payload={"handle": "Bob_Quiet"},
                    result={"verified": True, "changed": True, "state": "following", "handle": "Bob_Quiet"},
                )
                bridge._append_evidence(data, "follow_profile_verified", "act_f1", {"handle": "Alice_Builder"})
                bridge._write(data)

            mock_browser = MagicMock()
            mock_browser.open.return_value = {
                "url": "https://x.com/notifications",
                "text": "Notifications",
                "articles": [
                    {
                        "author": "@Alice_Builder",
                        "text": "Alice_Builder followed you",
                        "links": ["https://x.com/Alice_Builder"],
                    },
                    {
                        "author": "@Carol_Dev",
                        "text": "Replying to @example_creator Nice writeup on MCP locking",
                        "links": ["https://x.com/Carol_Dev/status/1900001"],
                    },
                    {
                        "author": "@Dave_Ops",
                        "text": "Dave_Ops liked your post",
                        "links": ["https://x.com/Dave_Ops"],
                    },
                ],
            }
            notif_res = browser_read.read_notifications(mock_browser, limit=10)
            kinds = {item["author"]: item["kind"] for item in notif_res["interactions"]}
            self.assertEqual(kinds["Alice_Builder"], "follow")
            self.assertEqual(kinds["Carol_Dev"], "reply")
            self.assertEqual(kinds["Dave_Ops"], "like")

            follow_handles = [item["author"] for item in notif_res["interactions"] if item["kind"] == "follow"]
            rec = bridge.record_mutual_followers(follow_handles, source="notifications_read")
            self.assertEqual(rec["added"], 1)
            self.assertEqual(rec["total"], 1)

            evidence = bridge.evidence()
            self.assertTrue(evidence["valid"])

            snap = bridge.read_ledger_snapshot()
            summary = build_ops_summary(snap, {"enabled": False})
            self.assertEqual(summary["follow_funnel"]["verified_follows"], 2)
            self.assertEqual(summary["follow_funnel"]["mutual_verified"], 1)
            self.assertEqual(summary["follow_funnel"]["known_inbound_followers"], 1)
            self.assertEqual(summary["follow_funnel"]["follow_back_rate"], 0.5)

            unfollow_cands = find_unfollow_candidates(snap, min_age_days=3)
            unfollow_handles = [c["handle"].casefold() for c in unfollow_cands]
            self.assertNotIn("alice_builder", unfollow_handles)
            self.assertIn("bob_quiet", unfollow_handles)

    def test_author_bundling_and_reply_then_follow_strategy(self) -> None:
        ledger = {
            "account_binding": "example_creator",
            "actions": {
                f"old_post_{i}": _action(
                    f"old_post_{i}",
                    "publish_post",
                    completed_at="2026-11-18T10:00:00+00:00",
                    result={"verified": True},
                )
                for i in range(5)
            },
        }
        snapshot = {"username": "example_creator", "followers_count": 320, "following_count": 410, "posts_count": 120}
        posts = [
            {
                "url": "https://x.com/inbound_alice/status/200001",
                "author": "inbound_alice",
                "text": "Thanks for sharing the lock benchmark, checking your MCP server repo now!",
                "source": "notification",
                "notif_kind": "follow",
                "visited_profile": True,
            },
            {
                "url": "https://x.com/visit_bob/status/200002",
                "author": "visit_bob",
                "text": "Benchmarking browser locator fallback vs raw API polling.",
                "source": "profile_visit",
                "visited_profile": True,
            },
        ]
        cands = [
            {"handle": "inbound_alice", "source": "notification_followback", "mutual_verified": True, "active": True, "visited_profile": True},
            {"handle": "visit_bob", "source": "cross_lang_discovery", "active": True, "visited_profile": True},
        ]

        plan_recip = build_daily_orchestration_plan(
            ledger,
            timeline_posts=posts,
            follow_candidates=cands,
            account_snapshot=snapshot,
            persona_topics=["mcp", "benchmark", "lock"],
            auto_reply=True,
            interaction_strategy="reciprocal_first",
        )
        self.assertEqual(plan_recip["interaction_strategy"], "reciprocal_first")
        self.assertGreaterEqual(plan_recip["bundled_count"], 4)
        ops_recip = plan_recip["operations"]
        self.assertEqual(ops_recip[0].get("interaction_role"), "inbound_followback")
        alice_actions = [op["action"] for op in ops_recip if (op.get("author") or op.get("handle") or "").casefold() == "inbound_alice"]
        self.assertEqual(alice_actions, ["follow_profile", "like_post", "reply_post"])
        bob_actions_recip = [op["action"] for op in ops_recip if (op.get("author") or op.get("handle") or "").casefold() == "visit_bob"]
        self.assertEqual(bob_actions_recip, ["like_post", "reply_post", "follow_profile"])

        plan_chat_first = build_daily_orchestration_plan(
            ledger,
            timeline_posts=posts,
            follow_candidates=cands,
            account_snapshot=snapshot,
            persona_topics=["mcp", "benchmark", "lock"],
            auto_reply=True,
            interaction_strategy="reply_then_follow",
        )
        ops_chat = plan_chat_first["operations"]
        bob_bundled = [
            op for op in ops_chat
            if (op.get("author") or op.get("handle") or "").casefold() == "visit_bob"
            and op.get("interaction_role") == "outbound_bundle"
        ]
        self.assertEqual([op["action"] for op in bob_bundled], ["like_post", "reply_post"])

    def test_target_pool_wave_syncs_notifications_when_browser_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = XStarter(
                mode="browser",
                environ={
                    "X_STARTER_MODE": "browser",
                    "X_STARTER_ENABLE_BROWSER": "1",
                    "X_STARTER_STATE_DIR": tmpdir,
                },
            )
            try:
                app._owner_profile = Path(tmpdir) / "owner-prof"
                app._owner_profile.mkdir()
                with app._bridge()._lock:
                    st_data = app._bridge()._read()
                    st_data["actions"]["obs_init"] = _action(
                        "obs_init", "observe_account", result={"username": "example_creator", "posts_count": 77, "verified": True}
                    )
                    app._bridge()._write(st_data)

                prep = app.call_tool(
                    "x_autopilot",
                    {
                        "action": "prepare",
                        "days": 7,
                        "active_hours": [0, 24],
                        "max_per_wave": 4,
                        "interaction_strategy": "reciprocal_first",
                    },
                )
                app.call_tool(
                    "x_autopilot",
                    {"action": "activate", "window_id": prep["data"]["window_id"], "confirmation": prep["data"]["confirmation_phrase"]},
                )
                app.call_tool(
                    "x_target_pool",
                    {
                        "action": "ingest",
                        "candidates": [{"handle": "pool_dev_1", "active": True}],
                        "posts": [{"url": "https://x.com/pool_dev_1/status/300001", "author": "pool_dev_1", "text": "Tuning MCP lock latency today"}],
                    },
                )

                mock_browser = MagicMock()
                mock_browser.browser_family = "chromium"

                def fake_open(url: str):
                    if "/notifications" in url:
                        return {
                            "url": "https://x.com/notifications",
                            "text": "Notifications",
                            "articles": [
                                {
                                    "author": "@Inbound_Fan",
                                    "text": "Inbound_Fan followed you",
                                    "links": ["https://x.com/Inbound_Fan/status/300099"],
                                }
                            ],
                        }
                    return {
                        "url": url,
                        "text": "Profile",
                        "articles": [
                            {
                                "author": "@pool_dev_1",
                                "text": "MCP worker lock throughput benchmark results",
                                "links": ["https://x.com/pool_dev_1/status/300002"],
                            }
                        ],
                    }

                def fake_action(*, action_type, post_url, author, text, action_id, claim_token, lease_expires_epoch):
                    if action_type == "like_post":
                        return {"verified": True, "changed": True, "state": "liked", "post_url": post_url, "author": author}
                    if action_type == "reply_post":
                        return {
                            "verified": True,
                            "changed": True,
                            "state": "replied",
                            "post_url": post_url,
                            "reply_url": f"https://x.com/example_creator/status/{abs(hash(action_id)) % 1000000}",
                            "text": text,
                            "author": "example_creator",
                        }
                    return {
                        "verified": True,
                        "changed": True,
                        "state": "following",
                        "handle": author,
                        "profile_url": post_url,
                    }

                mock_browser.open.side_effect = fake_open
                mock_browser.execute_x_action.side_effect = fake_action

                def tracked_browser_for(arguments):
                    app._owner_session = arguments.get("_mcp_session_id")
                    app._browsers[app._owner_session] = mock_browser
                    return mock_browser

                with patch.object(app, "_browser_for", side_effect=tracked_browser_for):
                    res = app.call_tool(
                        "x_autopilot",
                        {"action": "run_wave", "force": True, "respect_pacing": False, "sync_notifications": True},
                    )
                self.assertTrue(res["ok"])
                self.assertTrue(res["data"]["ran"])
                self.assertEqual(res["data"]["notifs_seen"], 1)
                self.assertEqual(res["data"]["mutuals_synced"], 1)
                self.assertEqual(res["data"]["interaction_strategy"], "reciprocal_first")
                self.assertGreater(res["data"]["bundled_count"], 0)
                snap = app._bridge().read_ledger_snapshot()
                self.assertIn("inbound_fan", snap.get("mutual_followers", {}))
            finally:
                app.close()

    def test_elite_tech_repost_filter_and_high_engagement_like_reply_scaling(self) -> None:
        from account_orchestrator import check_reply_naturalness, score_technical_quality

        chatter_post = {"text": "下午好！出摊啦，蓝V 活人一枚，想多认识点新朋友来聊天交朋友～ #AI"}
        short_post = {"text": "gm builders! cool demo today!"}
        tech_post_en = {
            "text": "Benchmarked our Rust tokio state machine under 200 concurrent locks: cutting DOM polling loops dropped p99 latency from 120ms to 14ms.",
            "author_bio": "Systems engineer building Rust + MCP runtime",
        }
        tech_post_zh = {
            "text": "把 MCP 调度器的并发锁与限流熔断重构成最小状态机，压测 429 边界后 p99 延迟从 120ms 降到 18ms，附开源实现复盘。",
            "author_bio": "系统架构 / 开源自动化工具链",
        }

        self.assertFalse(score_technical_quality(chatter_post)[1])
        self.assertFalse(score_technical_quality(short_post)[1])
        s_en, ok_en, r_en = score_technical_quality(tech_post_en)
        s_zh, ok_zh, r_zh = score_technical_quality(tech_post_zh)
        self.assertTrue(ok_en, r_en)
        self.assertTrue(ok_zh, r_zh)
        self.assertGreaterEqual(s_en, 38)
        self.assertGreaterEqual(s_zh, 38)

        # Build a realistic mix: 50 casual/non-tech posts + 3 elite tech posts + 25 follow candidates.
        ledger = {"account_binding": "example_creator", "actions": {}}
        timeline_posts = [
            {"url": "https://x.com/chatter_1/status/500001", "author": "chatter_1", "text": chatter_post["text"]},
            {"url": "https://x.com/chatter_2/status/500002", "author": "chatter_2", "text": short_post["text"]},
            {"url": "https://x.com/elite_en/status/500003", "author": "elite_en", "text": tech_post_en["text"], "author_bio": tech_post_en["author_bio"]},
            {"url": "https://x.com/elite_zh/status/500004", "author": "elite_zh", "text": tech_post_zh["text"], "author_bio": tech_post_zh["author_bio"]},
        ] + [
            {
                "url": f"https://x.com/casual_{i}/status/{500100 + i}",
                "author": f"casual_{i}",
                "text": f"Working on a small side project script update #{i} and sharing daily notes!",
            }
            for i in range(45)
        ] + [
            {
                "url": f"https://x.com/builder_mcp_{j}/status/{500200 + j}",
                "author": f"builder_mcp_{j}",
                "text": f"Debugging MCP Playwright DOM selector fallback and lock isolation on day {j} in our worker queue.",
            }
            for j in range(45)
        ]
        cands = [{"handle": f"cand_{k}", "bio": "Systems engineer", "active": True} for k in range(30)]

        plan = build_daily_orchestration_plan(
            ledger,
            goals={"publish_post": 0, "like_post": 120, "follow_profile": 200, "repost_post": 4, "reply_post": 45},
            timeline_posts=timeline_posts,
            follow_candidates=cands,
            account_snapshot={"posts_count": 90, "followers_count": 300, "following_count": 400},
            persona_topics=["mcp", "rust", "playwright"],
            auto_reply=True,
            interaction_strategy="reciprocal_first",
            seed=19,
        )
        # Likes & replies scale cleanly above the legacy 60/15 ceiling; reposts admit only elite tech posts!
        self.assertGreater(plan["counts"]["like_post"], 60)
        self.assertGreater(plan["counts"]["reply_post"], 20)
        repost_ops = [op for op in plan["operations"] if op["action"] == "repost_post"]
        self.assertGreaterEqual(len(repost_ops), 1)
        self.assertLessEqual(len(repost_ops), 4)
        for rop in repost_ops:
            self.assertNotIn("chatter", rop["author"])
            self.assertNotIn("casual_", rop["author"])
            self.assertGreaterEqual(int(rop.get("quality_score") or 0), 38)

        # In the first 12-action wave slice, replies+likes are substantially represented and reposts <= 1.
        wave12 = plan["operations"][:12]
        engage_wave = sum(1 for op in wave12 if op["action"] in {"like_post", "reply_post"})
        repost_wave = sum(1 for op in wave12 if op["action"] == "repost_post")
        self.assertGreaterEqual(engage_wave, 4)
        self.assertLessEqual(repost_wave, 1)

        # Check contextual replies add domain engineering insight cleanly with zero AI cliches.
        reply_texts = [op["text"] for op in plan["operations"] if op["action"] == "reply_post"]
        for txt in reply_texts[:10]:
            self.assertEqual(check_reply_naturalness(txt), [])

    def test_casual_banter_voice_and_short_fragment_skip(self) -> None:
        from account_orchestrator import clean_post_body, draft_friendly_reply

        # 1. Chrome DOM chrome must be stripped cleanly; "咋了" is only 2 chars -> MUST return None (no AI hallucination!)
        raw_short = "Sample A\n@user_a\n·\n12m\nReplying to \n@example_creator\n咋了"
        self.assertEqual(clean_post_body(raw_short), "咋了")
        self.assertIsNone(draft_friendly_reply({"text": raw_short, "source": "notification"}))

        # 2. Language learning chatter must stay conversational, zero architecture jargon
        raw_lang = "Sample B\n@user_b\n·\n3h\nReplying to \n@example_creator\n哈哈，学语言确实贵在坚持，常交流呀！\n7"
        rep_lang = draft_friendly_reply({"text": raw_lang, "source": "notification"})
        self.assertIsNotNone(rep_lang)
        assert rep_lang is not None
        self.assertNotIn("系统架构", rep_lang)
        self.assertNotIn("实战踩坑", rep_lang)
        self.assertNotIn("后验", rep_lang)

        # 3. Japanese follower milestone must stay pure Japanese, zero leaked "your architecture" English
        raw_jp_mile = "Sample C\n@user_c\n·\nNov 16\nフォロワーが300人超えました！\nありがとうございます\n\nジャンル問わず関わっていきたいので引き続きよろしくお願いします\n\n#ブルバ #ブルバ100 #相互フォロー\n134\n14\n403\n7.7K"
        rep_jp = draft_friendly_reply({"text": raw_jp_mile})
        self.assertIsNotNone(rep_jp)
        assert rep_jp is not None
        self.assertTrue(any("\u3040" <= ch <= "\u30ff" for ch in rep_jp), rep_jp)
        self.assertNotIn("your architecture", rep_jp.lower())
        self.assertNotIn("ログ", rep_jp)

        # 4. Japanese creative novel post must stay friendly creative encouragement, never tech benchmarking
        raw_jp_novel = "Sample D\n@user_d\n·\n3h\nフォロワー900人を達成しました。ありがとうございます。小説も読んで下さい。\n#小説家さんと繋がりたい\n22\n4\n49\n429"
        rep_novel = draft_friendly_reply({"text": raw_jp_novel})
        self.assertIsNotNone(rep_novel)
        assert rep_novel is not None
        self.assertNotIn("検証", rep_novel)

        # 5. Late-night bug/work venting uses relaxed 闲聊吐槽 tone without em-dash AI lectures
        rep_vent = draft_friendly_reply({"text": "凌晨两点改完一个报错又崩了三个地方，真破防了先睡了"})
        self.assertIsNotNone(rep_vent)
        assert rep_vent is not None
        self.assertNotIn("——", rep_vent)
        self.assertNotIn("实战踩坑", rep_vent)

        # 6. Ambiguous unrecognized off-topic text fails closed (returns None instead of gambling on a weird template)
        self.assertIsNone(draft_friendly_reply({"text": "刚才路过一家面包店门前排队人好多啊"}))

    def test_multi_strategy_orchestration_shapes_waves_differently(self) -> None:
        ledger = {"account_binding": "example_creator", "actions": {}}
        snapshot = {"username": "example_creator", "followers_count": 320, "following_count": 300, "posts_count": 120}
        queue_items = [
            {"item_id": "q_orig_1", "kind": "original_post", "status": "queued", "topic": "mcp", "text": "MCP browser executor shipping note #1"},
            {"item_id": "q_orig_2", "kind": "original_post", "status": "queued", "topic": "rust", "text": "Rust lock contention benchmark #2"},
        ]
        posts = [
            {
                "url": "https://x.com/engaged_dev/status/700001",
                "author": "engaged_dev",
                "text": "Debugging MCP lock timeouts and browser playwright selectors today.",
                "author_bio": "Systems builder",
                "visited_profile": True,
            },
            {
                "url": "https://x.com/rust_peer/status/700002",
                "author": "rust_peer",
                "text": "Benchmarked Rust tokio state machine cutting DOM polling latency from 120ms to 14ms.",
                "author_bio": "Systems engineer",
            },
        ]
        cands = [
            {"handle": "engaged_dev", "bio": "Systems builder", "followers_count": 400, "following_count": 350, "active": True, "visited_profile": True},
            {"handle": "rust_peer", "bio": "Systems engineer", "followers_count": 500, "following_count": 420, "active": True},
            {"handle": "cold_1", "bio": "Builder", "followers_count": 150, "following_count": 140, "active": True},
            {"handle": "cold_2", "bio": "Dev 互关", "followers_count": 200, "following_count": 210, "active": True},
            {"handle": "cold_3", "bio": "Hacker 互follow", "followers_count": 180, "following_count": 190, "active": True},
            {"handle": "cold_4", "bio": "Designer", "followers_count": 250, "following_count": 220, "active": True},
            {"handle": "cold_5", "bio": "Coder", "followers_count": 300, "following_count": 280, "active": True},
            {"handle": "cold_6", "bio": "Indie", "followers_count": 120, "following_count": 100, "active": True},
        ]

        plan_sniper = build_daily_orchestration_plan(
            ledger,
            goals={"publish_post": 1, "like_post": 10, "follow_profile": 10, "repost_post": 2, "reply_post": 5},
            timeline_posts=posts,
            follow_candidates=cands,
            content_queue=queue_items,
            account_snapshot=snapshot,
            persona_topics=["mcp", "rust"],
            auto_reply=True,
            interaction_strategy="niche_sniper_bundle",
            seed=7,
        )
        sniper_follows = [op["handle"].casefold() for op in plan_sniper["operations"] if op["action"] == "follow_profile"]
        self.assertEqual(set(sniper_follows), {"engaged_dev", "rust_peer"})

        plan_magnet = build_daily_orchestration_plan(
            ledger,
            goals={"publish_post": 2, "like_post": 10, "follow_profile": 12, "repost_post": 2, "reply_post": 5},
            timeline_posts=posts,
            follow_candidates=cands,
            content_queue=queue_items,
            account_snapshot=snapshot,
            persona_topics=["mcp", "rust"],
            auto_reply=True,
            interaction_strategy="thread_reply_magnet",
            seed=7,
        )
        self.assertEqual(plan_magnet["counts"]["publish_post"], 2)
        self.assertLessEqual(plan_magnet["counts"]["follow_profile"], 3)

        plan_recip = build_daily_orchestration_plan(
            ledger,
            goals={"publish_post": 1, "like_post": 10, "follow_profile": 10, "repost_post": 2, "reply_post": 5},
            timeline_posts=posts,
            follow_candidates=cands,
            content_queue=queue_items,
            account_snapshot=snapshot,
            persona_topics=["mcp", "rust"],
            auto_reply=True,
            interaction_strategy="reciprocal_first",
            seed=7,
        )
        self.assertEqual(plan_recip["counts"]["follow_profile"], len(cands))

    def test_follower_growth_trend_and_auto_fastest_strategy_routing(self) -> None:
        from datetime import datetime, timezone
        from autopilot_ops import (
            AutopilotStore,
            build_ops_summary,
            evaluate_strategy_performance,
            persist_daily_metrics,
            select_best_growth_strategy,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = XStarter(
                mode="browser",
                environ={
                    "X_STARTER_MODE": "browser",
                    "X_STARTER_ENABLE_BROWSER": "1",
                    "X_STARTER_STATE_DIR": str(root),
                },
            )
            try:
                # Seed chronological follower snapshots & waves showing `niche_sniper_bundle`
                # decisively outperforms `reciprocal_first` in both follower delta & sticky inbound followers.
                now_ep = time.time()
                day1_ep = now_ep - 3 * 86400
                day2_ep = now_ep - 2 * 86400
                day3_ep = now_ep - 1 * 86400
                day1_iso = datetime.fromtimestamp(day1_ep, tz=timezone.utc).isoformat()
                day2_iso = datetime.fromtimestamp(day2_ep, tz=timezone.utc).isoformat()
                day3_iso = datetime.fromtimestamp(day3_ep, tz=timezone.utc).isoformat()

                bridge = app._bridge()
                bridge.record_account(
                    username="example_creator",
                    user_id="101",
                    display_name="Example",
                    posts_count=80,
                    recent_post_ids=[],
                    followers_count=100,
                    following_count=120,
                    strategy="reciprocal_first",
                )
                with bridge._lock:
                    data = bridge._read()
                    data["follower_snapshots"] = [
                        {"at": day1_iso, "at_epoch": day1_ep, "date": datetime.fromtimestamp(day1_ep).astimezone().date().isoformat(), "followers_count": 100, "following_count": 120, "strategy": "reciprocal_first", "source": "account_observed"},
                        {"at": day2_iso, "at_epoch": day2_ep, "date": datetime.fromtimestamp(day2_ep).astimezone().date().isoformat(), "followers_count": 102, "following_count": 124, "strategy": "reciprocal_first", "source": "account_observed"},
                        {"at": day3_iso, "at_epoch": day3_ep, "date": datetime.fromtimestamp(day3_ep).astimezone().date().isoformat(), "followers_count": 115, "following_count": 126, "strategy": "niche_sniper_bundle", "source": "account_observed"},
                        {"at": datetime.fromtimestamp(now_ep, tz=timezone.utc).isoformat(), "at_epoch": now_ep, "date": datetime.fromtimestamp(now_ep).astimezone().date().isoformat(), "followers_count": 126, "following_count": 128, "strategy": "niche_sniper_bundle", "source": "account_observed"},
                    ]
                    data["mutual_followers"] = {
                        "fan_1": {"handle": "fan_1", "verified_at": day1_iso, "last_seen_at": day3_iso, "strategy": "reciprocal_first", "source": "notifications_read"},
                        "sticky_1": {"handle": "sticky_1", "verified_at": day2_iso, "last_seen_at": datetime.fromtimestamp(now_ep, tz=timezone.utc).isoformat(), "strategy": "niche_sniper_bundle", "source": "notifications_read"},
                        "sticky_2": {"handle": "sticky_2", "verified_at": day2_iso, "last_seen_at": datetime.fromtimestamp(now_ep, tz=timezone.utc).isoformat(), "strategy": "niche_sniper_bundle", "source": "notifications_read"},
                        "sticky_3": {"handle": "sticky_3", "verified_at": day3_iso, "last_seen_at": datetime.fromtimestamp(now_ep, tz=timezone.utc).isoformat(), "strategy": "niche_sniper_bundle", "source": "notifications_read"},
                        "sticky_4": {"handle": "sticky_4", "verified_at": datetime.fromtimestamp(now_ep, tz=timezone.utc).isoformat(), "last_seen_at": datetime.fromtimestamp(now_ep, tz=timezone.utc).isoformat(), "strategy": "niche_sniper_bundle", "source": "notifications_read"},
                    }
                    bridge._write(data)

                store = app._autopilot_store()
                prep = app.call_tool("x_autopilot", {
                    "action": "prepare",
                    "days": 7,
                    "active_hours": [0, 24],
                    "max_per_wave": 6,
                    "interaction_strategy": "auto_fastest",
                })
                self.assertEqual(prep["data"]["interaction_strategy"], "auto_fastest")
                app.call_tool("x_autopilot", {
                    "action": "activate",
                    "window_id": prep["data"]["window_id"],
                    "confirmation": prep["data"]["confirmation_phrase"],
                })

                # Record 2 waves on reciprocal_first (+2 total) and 2 waves on niche_sniper_bundle (+24 total).
                store.record_wave({
                    "batch_id": "w_r1", "interaction_strategy": "reciprocal_first",
                    "processed": 4, "verified_changed": 4, "uncertain_count": 1,
                    "action_counts": {"follow_profile": 3, "like_post": 1},
                    "followers_before": 100, "followers_after": 101,
                }, now_epoch=day1_ep + 60)
                store.record_wave({
                    "batch_id": "w_r2", "interaction_strategy": "reciprocal_first",
                    "processed": 4, "verified_changed": 3, "uncertain_count": 1,
                    "action_counts": {"follow_profile": 2, "like_post": 1},
                    "followers_before": 101, "followers_after": 102,
                }, now_epoch=day1_ep + 3600)
                store.record_wave({
                    "batch_id": "w_n1", "interaction_strategy": "niche_sniper_bundle",
                    "processed": 6, "verified_changed": 6, "uncertain_count": 0,
                    "action_counts": {"follow_profile": 2, "reply_post": 2, "like_post": 2},
                    "followers_before": 102, "followers_after": 115,
                }, now_epoch=day2_ep + 60)
                store.record_wave({
                    "batch_id": "w_n2", "interaction_strategy": "niche_sniper_bundle",
                    "processed": 6, "verified_changed": 6, "uncertain_count": 0,
                    "action_counts": {"follow_profile": 2, "reply_post": 2, "like_post": 2},
                    "followers_before": 115, "followers_after": 126,
                }, now_epoch=day3_ep + 60)

                summary_res = app.call_tool("x_autopilot", {"action": "ops_summary"})
                self.assertTrue(summary_res["ok"])
                summary = summary_res["data"]
                trend = summary["follower_trend"]
                self.assertEqual(trend["current_followers"], 126)
                self.assertEqual(trend["net_change_7d"], 26)
                self.assertEqual(trend["data_source"], "profile_snapshots")
                self.assertGreaterEqual(trend["sticky_followers"], 3)
                self.assertEqual(summary["fastest_strategy"]["confidence"], "measured")
                self.assertEqual(summary["fastest_strategy"]["recommended_strategy"], "niche_sniper_bundle")

                # Verify `auto_fastest` resolves to the empirical winner on normal waves (wave_index=0).
                decision = select_best_growth_strategy(
                    app._bridge().read_ledger_snapshot(),
                    autopilot_status=store.status(now_epoch=now_ep),
                    wave_index=0,
                    now_epoch=now_ep,
                )
                self.assertEqual(decision["recommended_strategy"], "niche_sniper_bundle")
                self.assertEqual(decision["selection_mode"], "empirical_winner")

                # Verify `set_strategy` switches cleanly without requiring a new approval window.
                switched = app.call_tool("x_autopilot", {
                    "action": "set_strategy",
                    "interaction_strategy": "thread_reply_magnet",
                })
                self.assertTrue(switched["ok"])
                self.assertEqual(switched["data"]["interaction_strategy"], "thread_reply_magnet")
            finally:
                app.close()

    def test_early_drop_by_orchestration_post_analysis_and_casual_templates(self) -> None:
        from account_orchestrator import (
            analyze_target_post,
            check_reply_naturalness,
            get_casual_interaction_templates,
        )

        # 1. Pre-drafted template catalog must have all core scenarios and zero AI boilerplate.
        catalog = get_casual_interaction_templates()
        self.assertGreaterEqual(len(catalog["scenarios"]), 5)
        for scenario in catalog["scenarios"]:
            if scenario["id"] == "skip_reply_like_only":
                continue
            for key in ("templates_zh", "templates_en", "templates_ja"):
                for line in scenario.get(key) or []:
                    self.assertEqual(check_reply_naturalness(line), [], f"AI/unsafe template: {line}")

        # 2. Brief target-post analysis detects mood, care angle, and skips ambiguous fragments.
        late = analyze_target_post({"text": "Sample\n@alice\n·\n2h\n凌晨两点加班修个报错，脑子都要炸了\n4\n12"})
        self.assertEqual(late["intent"], "vent_rant")
        self.assertIn("late_night", late["life_hooks"])
        self.assertEqual(late["drop_by_tactic"], "drop_by_like_1_to_2_and_warm_reply")
        self.assertIn("睡", late["care_angle"])

        short_skip = analyze_target_post({"text": "Sample\n@bob\n·\n5m\n咋了"})
        self.assertEqual(short_skip["drop_by_tactic"], "like_only_skip_reply")

        # 3. Early-stage (`light_engage`) unlocks drop-by replies & allows 2 likes + 1 reply per visited neighbor.
        ledger = {"account_binding": "example_creator", "actions": {}}
        early_snap = {"username": "example_creator", "posts_count": 4, "account_age_days": 5, "bio": "Coding notes"}
        early_posts = [
            {
                "url": "https://x.com/neighbor_a/status/810001",
                "author": "neighbor_a",
                "text": "周末喝杯冰美式摸鱼喘口气，平时看电脑眼睛快花了哈哈",
                "visited_profile": True,
                "reply_count": 1,
                "author_followers": 280,
            },
            {
                "url": "https://x.com/neighbor_a/status/810002",
                "author": "neighbor_a",
                "text": "最近也在慢慢折腾小工具，有活人路过欢迎聊天～",
                "visited_profile": True,
                "reply_count": 2,
                "author_followers": 280,
            },
            {
                "url": "https://x.com/mega_account/status/810099",
                "author": "mega_account",
                "text": "Good morning coffee builders! Happy weekend!",
                "visited_profile": False,
                "reply_count": 240,
                "author_followers": 120000,
            },
        ]
        plan = build_daily_orchestration_plan(
            ledger,
            goals={"publish_post": 1, "like_post": 10, "follow_profile": 2, "reply_post": 4},
            timeline_posts=early_posts,
            follow_candidates=[{"handle": "neighbor_a", "visited_profile": True, "followers_count": 280, "following_count": 240}],
            account_snapshot=early_snap,
            auto_reply=True,
            interaction_strategy="niche_sniper_bundle",
            seed=11,
        )
        self.assertEqual(plan["stage"], "light_engage")
        self.assertGreaterEqual(plan["counts"]["reply_post"], 1)
        neighbor_likes = [op for op in plan["operations"] if op["action"] == "like_post" and op.get("author") == "neighbor_a"]
        neighbor_replies = [op for op in plan["operations"] if op["action"] == "reply_post" and op.get("author") == "neighbor_a"]
        self.assertEqual(len(neighbor_likes), 2)
        self.assertEqual(len(neighbor_replies), 1)
        self.assertIn("post_analysis", neighbor_replies[0])
        self.assertEqual(check_reply_naturalness(neighbor_replies[0]["text"]), [])

        # 4. MCP tool `x_persona_memory` exposes `interaction_templates` and `analyze_post`.
        with tempfile.TemporaryDirectory() as tmpdir:
            app = XStarter(
                mode="browser",
                environ={"X_STARTER_MODE": "browser", "X_STARTER_ENABLE_BROWSER": "1", "X_STARTER_STATE_DIR": tmpdir},
            )
            try:
                tpl_res = app.call_tool("x_persona_memory", {"action": "interaction_templates"})
                self.assertTrue(tpl_res["ok"])
                self.assertGreaterEqual(len(tpl_res["data"]["scenarios"]), 5)

                an_res = app.call_tool(
                    "x_persona_memory",
                    {"action": "analyze_post", "note": "加班到凌晨两点还在调一个报错，头都大了先喝口水"},
                )
                self.assertTrue(an_res["ok"])
                self.assertEqual(an_res["data"]["intent"], "vent_rant")
                self.assertGreaterEqual(len(an_res["data"]["sample_replies"]), 1)
                for sr in an_res["data"]["sample_replies"]:
                    self.assertEqual(check_reply_naturalness(sr), [])
            finally:
                app.close()

    def test_rolling_rate_limit_windows_and_standalone_post_spacing(self) -> None:
        from datetime import datetime, timezone
        from account_orchestrator import (
            X_RATE_LIMIT_REFERENCE,
            evaluate_rolling_windows,
            slice_wave_for_rate_limits,
        )

        base_ep = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        # Seed 10 follows within the past 8 minutes (hitting safe 15m cap of 10, safely below X's 15/900s wall).
        ledger = {
            "account_binding": "example_creator",
            "actions": {
                f"f_{i}": _action(
                    f"f_{i}",
                    "follow_profile",
                    completed_at=datetime.fromtimestamp(base_ep - 480 + i * 30, tz=timezone.utc).isoformat(),
                    payload={"handle": f"peer_{i}", "profile_url": f"https://x.com/peer_{i}"},
                    result={"handle": f"peer_{i}", "profile_url": f"https://x.com/peer_{i}", "state": "following", "verified": True, "changed": True},
                )
                for i in range(X_RATE_LIMIT_REFERENCE["follow_profile"]["safe_per_15m"])
            },
        }
        rw = evaluate_rolling_windows(ledger, now_epoch=base_ep)
        self.assertEqual(rw["rolling_remaining"]["follow_profile"], 0)
        self.assertIsNotNone(rw["next_reset_epoch"]["follow_profile"])
        self.assertGreater(rw["next_reset_epoch"]["follow_profile"], base_ep)

        # Health report surfaces rolling_windows and advises wait_rolling_window instead of blind retry.
        health = evaluate_account_health(
            ledger,
            account_snapshot={"username": "example_creator", "posts_count": 80, "has_avatar": True, "bio": "Builder"},
            now_epoch=base_ep,
        )
        self.assertEqual(health["rolling_windows"]["rolling_remaining"]["follow_profile"], 0)
        wait_recs = [r for r in health["recommended_actions"] if r.get("action") == "wait_rolling_window"]
        self.assertEqual(len(wait_recs), 1)
        self.assertEqual(wait_recs[0]["target_action"], "follow_profile")

        # Two consecutive standalone queued posts must be spaced >= 1800s apart, and immediate slice allows only 1 standalone post + remaining likes.
        plan = build_daily_orchestration_plan(
            ledger,
            goals={"publish_post": 2, "like_post": 3, "follow_profile": 10},
            content_queue=[
                {"item_id": "p1", "kind": "original_post", "status": "queued", "text": "Standalone post one"},
                {"item_id": "p2", "kind": "original_post", "status": "queued", "text": "Standalone post two"},
            ],
            timeline_posts=[{"url": "https://x.com/alice/status/901", "author": "alice", "text": "Shipping MCP today"}],
            follow_candidates=[{"handle": "cold_extra", "bio": "builder"}],
            account_snapshot={"username": "example_creator", "posts_count": 80, "has_avatar": True, "bio": "Builder"},
            now_epoch=base_ep,
            seed=5,
        )
        pub_ops = [op for op in plan["operations"] if op["action"] == "publish_post"]
        self.assertEqual(len(pub_ops), 2)
        self.assertGreaterEqual(pub_ops[1]["delay_before_seconds"], 1800)

        sliced = slice_wave_for_rate_limits(plan["operations"], rw, wave_limit=10)
        sliced_actions = [op["action"] for op in sliced["operations"]]
        self.assertNotIn("follow_profile", sliced_actions)
        self.assertEqual(sliced_actions.count("publish_post"), 1)
        self.assertIn("like_post", sliced_actions)
        self.assertEqual(sliced["skipped_by_rate_limit"].get("follow_profile"), 1)

        # Autopilot run_wave refuses cleanly with ROLLING_RATE_LIMIT_PACING when rolling window is full for all eligible ops.
        with tempfile.TemporaryDirectory() as tmpdir:
            app = XStarter(
                mode="browser",
                environ={"X_STARTER_MODE": "browser", "X_STARTER_ENABLE_BROWSER": "1", "X_STARTER_STATE_DIR": tmpdir},
            )
            try:
                prep = app.call_tool("x_autopilot", {"action": "prepare", "days": 7})
                # Default prepare without explicit follow goal uses safer 40/day & wave<=10.
                self.assertEqual(prep["data"]["daily_goals"]["follow_profile"], 40)
                self.assertLessEqual(prep["data"]["max_per_wave"], 10)
                app.call_tool(
                    "x_autopilot",
                    {"action": "activate", "window_id": prep["data"]["window_id"], "confirmation": prep["data"]["confirmation_phrase"]},
                )
                with app._bridge()._lock:
                    st = app._bridge()._read()
                    now_ts = time.time()
                    for i in range(10):
                        st["actions"][f"rl_{i}"] = _action(
                            f"rl_{i}",
                            "follow_profile",
                            completed_at=datetime.fromtimestamp(now_ts - 60 - i * 10, tz=timezone.utc).isoformat(),
                            payload={"handle": f"done_{i}", "profile_url": f"https://x.com/done_{i}"},
                            result={"handle": f"done_{i}", "profile_url": f"https://x.com/done_{i}", "state": "following", "verified": True, "changed": True},
                        )
                    app._bridge()._write(st)

                mock_browser = MagicMock()
                mock_browser.browser_family = "chromium"
                with patch.object(app, "_browser_for", return_value=mock_browser):
                    res = app.call_tool(
                        "x_autopilot",
                        {
                            "action": "run_wave",
                            "force": True,
                            "timeline_posts": [],
                            "follow_candidates": [{"handle": "fresh_target", "bio": "builder"}],
                            "account_snapshot": {"username": "example_creator", "posts_count": 80, "has_avatar": True, "bio": "Builder"},
                        },
                    )
                self.assertTrue(res["ok"])
                self.assertFalse(res["data"]["ran"])
                self.assertEqual(res["data"]["reason"], "ROLLING_RATE_LIMIT_PACING")
                self.assertGreaterEqual(res["data"]["skipped_by_rate_limit"].get("follow_profile", 0), 1)
                self.assertFalse(mock_browser.execute_x_action.called)
            finally:
                app.close()

    def test_high_volume_proactive_reply_orchestration_200_to_400(self) -> None:
        from account_orchestrator import (
            X_RATE_LIMIT_REFERENCE,
            check_reply_naturalness,
            draft_friendly_reply,
            evaluate_rolling_windows,
            slice_wave_for_rate_limits,
        )
        from browser_bridge import BrowserBridge

        ledger = {"account_binding": "example_creator", "actions": {}}
        # 1. Warmup stages MUST keep blocking 200-400 reply bursts (<=5 / <=12).
        sample_posts = [
            {"url": f"https://x.com/wpeer_{i}/status/{100 + i}", "author": f"wpeer_{i}", "text": f"Debugging MCP edge timeout case #{i}"}
            for i in range(30)
        ]
        light_plan = build_daily_orchestration_plan(
            ledger,
            goals={"publish_post": 0, "like_post": 30, "follow_profile": 10, "repost_post": 0, "reply_post": 400},
            timeline_posts=sample_posts,
            account_snapshot={"posts_count": 4, "followers_count": 15, "following_count": 30},
            auto_reply=True,
            seed=7,
        )
        self.assertEqual(light_plan["stage"], "light_engage")
        self.assertLessEqual(light_plan["counts"]["reply_post"], 5)

        content_plan = build_daily_orchestration_plan(
            ledger,
            goals={"publish_post": 0, "like_post": 60, "follow_profile": 20, "repost_post": 0, "reply_post": 400},
            timeline_posts=sample_posts,
            account_snapshot={"posts_count": 16, "followers_count": 60, "following_count": 70},
            auto_reply=True,
            seed=7,
        )
        self.assertEqual(content_plan["stage"], "content_build")
        self.assertLessEqual(content_plan["counts"]["reply_post"], 12)

        # 2. Established growth account plans 300+ unique, natural non-AI proactive replies (plus preserves too_short/unknown failsafe).
        big_posts = []
        for i in range(340):
            kind = i % 6
            if kind == 0:
                txt = f"最近写 MCP 并发脚本时第 {i} 次遇到边界超时，半夜查日志真的头大"
            elif kind == 1:
                txt = f"debugging Playwright lock timeout case {i} tonight — silent skips eat the whole evening"
            elif kind == 2:
                txt = f"凌晨一点还在调部署脚本，好累啊先泡杯咖啡歇口气 #{i}"
            elif kind == 3:
                txt = f"ugh 1am and my local test keeps flaking on worker #{i}, brain is fried"
            elif kind == 4:
                txt = f"午后喝杯冰美式摸会儿鱼，刷到活人随手打个招呼 #{i}"
            else:
                txt = f"taking a quiet coffee break scrolling the feed today, nice seeing real folks #{i}"
            big_posts.append({
                "url": f"https://x.com/neighbor_{i}/status/{50000 + i}",
                "author": f"neighbor_{i}",
                "text": txt,
                "author_bio": "Builder & indie dev",
            })
        big_posts.append({"url": "https://x.com/short_guy/status/999001", "author": "short_guy", "text": "咋了"})
        big_posts.append({"url": "https://x.com/bakery_guy/status/999002", "author": "bakery_guy", "text": "刚才路过一家面包店门前排队人好多啊"})

        growth_plan = build_daily_orchestration_plan(
            ledger,
            goals={"publish_post": 0, "like_post": 150, "follow_profile": 40, "repost_post": 2, "reply_post": 400},
            timeline_posts=big_posts,
            follow_candidates=[{"handle": f"neighbor_{i}", "bio": "dev"} for i in range(60)],
            account_snapshot={"posts_count": 120, "followers_count": 400, "following_count": 420},
            persona_topics=["mcp", "playwright"],
            auto_reply=True,
            interaction_strategy="reciprocal_first",
            seed=42,
        )
        self.assertFalse(growth_plan["blocked"])
        self.assertGreaterEqual(growth_plan["counts"]["reply_post"], 300)
        reply_ops = [op for op in growth_plan["operations"] if op["action"] == "reply_post"]
        reply_texts = [op["text"] for op in reply_ops]
        self.assertEqual(len(reply_texts), len(set(reply_texts)), "Expected zero duplicate reply texts across 300+ replies")
        for txt in reply_texts:
            self.assertEqual(check_reply_naturalness(txt), [], f"AI cliche or naturalness warning in: {txt}")
        replied_authors = {op["author"] for op in reply_ops}
        self.assertNotIn("short_guy", replied_authors)
        self.assertNotIn("bakery_guy", replied_authors)
        for op in reply_ops:
            if op["step_index"] > 1:
                self.assertGreaterEqual(op["delay_before_seconds"], 55)

        # 3. Single-language stress test: generate 250 unique replies for pure ZH tech_discussion with zero repeats.
        import random
        from datetime import datetime, timezone

        seen_single: list[str] = []
        rng = random.Random(99)
        for i in range(250):
            r_txt = draft_friendly_reply(
                {"text": f"今天又被并发状态机和 Playwright 超时折腾了一次 #{i}"},
                recent_replies=seen_single,
                rng=rng,
            )
            self.assertIsNotNone(r_txt, f"Ran out of varied ZH tech replies at #{i}")
            assert r_txt is not None
            self.assertEqual(check_reply_naturalness(r_txt), [])
            seen_single.append(r_txt)
        self.assertEqual(len(seen_single), len(set(seen_single)))

        # 4. Daily batch bridge accepts up to 800 operations cleanly & rolling 15m (10) / 1h (32) throttles bursts.
        self.assertEqual(X_RATE_LIMIT_REFERENCE["reply_post"]["safe_per_15m"], 10)
        self.assertEqual(X_RATE_LIMIT_REFERENCE["reply_post"]["safe_per_1h"], 32)
        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = BrowserBridge(Path(tmpdir))
            batch_res = bridge.prepare_daily_batch(growth_plan, session_id="s1")
            self.assertGreaterEqual(batch_res["operation_count"], 300)

        now_ep = 1795000000.0
        heavy_ledger = {
            "actions": {
                f"rep_{i}": _action(
                    f"rep_{i}",
                    "reply_post",
                    completed_at=datetime.fromtimestamp(now_ep - 120 - i * 20, tz=timezone.utc).isoformat(),
                    payload={"url": f"https://x.com/u{i}/status/{i}", "text": f"reply #{i}"},
                )
                for i in range(10)
            }
        }
        rw = evaluate_rolling_windows(heavy_ledger, now_epoch=now_ep)
        self.assertEqual(rw["used_15m"]["reply_post"], 10)
        self.assertEqual(rw["rolling_remaining"]["reply_post"], 0)
        self.assertIsNotNone(rw["next_reset_epoch"]["reply_post"])
        throttled = slice_wave_for_rate_limits(reply_ops[:5], rw, wave_limit=10)
        self.assertEqual(len(throttled["operations"]), 0)
        self.assertEqual(throttled["skipped_by_rate_limit"]["reply_post"], 5)

        # 5. XStarter autopilot prepare defaults to 240 replies/day (in 200-400 range) while respecting <=10 wave when goals omitted.
        with tempfile.TemporaryDirectory() as tmpdir:
            app = XStarter(
                mode="browser",
                environ={"X_STARTER_MODE": "browser", "X_STARTER_ENABLE_BROWSER": "1", "X_STARTER_STATE_DIR": tmpdir},
            )
            try:
                default_prep = app.call_tool("x_autopilot", {"action": "prepare", "days": 7})
                self.assertTrue(default_prep["ok"])
                self.assertEqual(default_prep["data"]["daily_goals"]["reply_post"], 240)
                self.assertEqual(default_prep["data"]["daily_goals"]["follow_profile"], 40)
                self.assertLessEqual(default_prep["data"]["max_per_wave"], 10)
                self.assertEqual(default_prep["data"]["min_wave_interval_minutes"], 15)

                high_prep = app.call_tool(
                    "x_autopilot",
                    {
                        "action": "prepare",
                        "days": 7,
                        "daily_goals": {"publish_post": 1, "like_post": 150, "follow_profile": 40, "repost_post": 2, "reply_post": 360},
                    },
                )
                self.assertTrue(high_prep["ok"])
                self.assertEqual(high_prep["data"]["daily_goals"]["reply_post"], 360)
                self.assertEqual(high_prep["data"]["max_per_wave"], 14)
                self.assertEqual(high_prep["data"]["min_wave_interval_minutes"], 12)
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
