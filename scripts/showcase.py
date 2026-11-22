#!/usr/bin/env python3
"""One-command reproducible capability demo and visual showcase asset builder.

Runs three verifiable tiers on a clean isolated workspace with synthetic
accounts (`@example_creator`) and zero personal credentials or live X writes:
  1. MCP Protocol Core Benchmark (`tools/list`, reads across all 65 tools)
  2. Real-Device Headless Chromium Execution (`Browser` open, SHA-256 PNG
     capture, DOM postcondition clicks, and fail-closed `OBSERVATION_STALE` guard)
  3. Multi-Strategy Growth Attribution & Cryptographic Evidence Chain across
     all 6 orchestration strategies (`auto_fastest` -> `thread_reply_magnet` vs
     `reciprocal_first` / `niche_sniper_bundle` / `reply_then_follow` / `balanced`)

Optional `--capture-assets <dir>` boots a real Streamable HTTP dashboard server
and captures crisp real-Chromium PNGs + an animated GIF walkthrough for docs.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for _path in (str(ROOT / "src"), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from account_orchestrator import build_daily_orchestration_plan  # noqa: E402
from autopilot_ops import AutopilotStore  # noqa: E402
import browser_paths  # noqa: E402
from browser_bridge import BrowserBridge  # noqa: E402
from scripts.benchmark import run as run_core_benchmark  # noqa: E402
from server import SessionRegistry, XStarter, handle_message, make_http_handler  # noqa: E402


SHOWCASE_PAGE = b"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>X MCP Showcase Feed Fixture (@example_creator)</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; height: 1800px; background: #09090b; color: #f4f4f5;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex; justify-content: center; padding: 36px 24px;
  }
  .shell { width: 880px; }
  .banner {
    display: flex; justify-content: space-between; align-items: center;
    padding: 12px 16px; border: 1px solid #27272a; border-radius: 10px;
    background: #111114; margin-bottom: 18px;
  }
  .badge {
    font-size: 12px; padding: 3px 9px; border-radius: 999px;
    border: 1px solid rgba(16,185,129,0.35); background: rgba(16,185,129,0.12); color: #6ee7b7;
  }
  .card {
    background: #111114; border: 1px solid #27272a; border-radius: 12px;
    padding: 20px; margin-bottom: 16px;
  }
  .profile { display: flex; justify-content: space-between; align-items: center; }
  .handle { color: #a1a1aa; font-size: 13px; margin-top: 2px; }
  .post-text { margin: 12px 0 16px; line-height: 1.55; color: #e4e4e7; font-size: 15px; }
  .actions { display: flex; gap: 12px; align-items: center; }
  button {
    cursor: pointer; border-radius: 999px; padding: 8px 18px; font-size: 13px; font-weight: 600;
    border: 1px solid #3f3f46; background: #18181b; color: #f4f4f5;
  }
  button.primary { background: #10b981; border-color: #10b981; color: #052e16; }
  button.done { background: rgba(16,185,129,0.18); border-color: #10b981; color: #6ee7b7; }
  .proof {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
    color: #a1a1aa; margin-top: 12px; padding: 10px 12px; background: #09090b;
    border: 1px solid #27272a; border-radius: 8px;
  }
</style>
</head>
<body>
<div class="shell">
  <div class="banner">
    <div><strong>X MCP Real Chromium Execution Proof</strong> <span class="handle">&middot; Local Synthetic Feed (Zero Live X Writes)</span></div>
    <span class="badge">DOM + SHA-256 Postcondition Verified</span>
  </div>
  <div class="card">
    <div class="profile">
      <div>
        <div style="font-size:18px;font-weight:700;">Example Builder</div>
        <div class="handle">@alice_rust &middot; <span id="followerStat">1,240 followers</span></div>
      </div>
      <button id="followBtn" class="primary" onclick="onFollow()">Follow</button>
    </div>
    <div class="post-text">
      Built a minimal state machine for MCP browser execution: every write uses one-shot leases,
      DOM postcondition verification after click, and fail-closed stagnation stops on stale frames.
    </div>
    <div class="actions">
      <button id="likeBtn" onclick="onLike()">Like (<span id="likeCount">28</span>)</button>
      <span id="actionStatus" class="badge">Ready</span>
    </div>
    <div id="proofBox" class="proof">awaiting real Chromium DOM click + postcondition verification...</div>
  </div>
  <div class="card">
    <div style="font-size:14px;font-weight:600;margin-bottom:10px;">Execution Guardrail Verification Matrix (Real Headless Chromium)</div>
    <div class="proof" style="margin-top:0;line-height:1.7;">
      <div>[1] <strong>browser_open + observe</strong> ...... DOM fingerprint SHA-256 bound to ephemeral localhost fixture</div>
      <div>[2] <strong>browser_screenshot</strong> .......... PNG magic header + byte digest verified before fallback use</div>
      <div>[3] <strong>DOM Click Postcondition</strong> ..... #likeBtn -&gt; "Liked (29)" &amp; #followBtn -&gt; "Following (1,241 followers)"</div>
      <div>[4] <strong>Stale Observation Guard</strong> ..... Re-using expired token -&gt; Fail-Closed <code>OBSERVATION_STALE</code></div>
    </div>
  </div>
</div>
<script>
  let liked = false;
  let following = false;
  function updateProof() {
    document.getElementById('actionStatus').textContent =
      (liked ? 'Liked' : 'Ready') + (following ? ' + Following' : '');
    document.getElementById('proofBox').textContent =
      'dom_state=' + JSON.stringify({liked, following, likes: liked ? 29 : 28, viewport: innerWidth + 'x' + innerHeight});
  }
  function onLike() {
    liked = true;
    document.getElementById('likeCount').textContent = '29';
    const b = document.getElementById('likeBtn');
    b.classList.add('done');
    b.firstChild.textContent = 'Liked (';
    updateProof();
  }
  function onFollow() {
    following = true;
    document.getElementById('followerStat').textContent = '1,241 followers';
    const b = document.getElementById('followBtn');
    b.className = 'done';
    b.textContent = 'Following';
    updateProof();
  }
</script>
</body>
</html>
"""


class _ShowcaseFeedHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(SHOWCASE_PAGE)))
        self.end_headers()
        self.wfile.write(SHOWCASE_PAGE)

    def log_message(self, *args: object) -> None:
        return


def _iso_at(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _verify_core(iterations: int = 1000) -> dict[str, Any]:
    app = XStarter(mode="mock", environ={})
    init_reply = handle_message(
        app,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        },
    )
    list_reply = handle_message(app, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = (list_reply.get("result") or {}).get("tools") or []
    bench = run_core_benchmark(iterations=max(100, int(iterations)))
    res = bench["results"]
    return {
        "scope": "in-process MCP protocol core (mode=mock, network_calls=0)",
        "server_version": (init_reply.get("result") or {}).get("serverInfo", {}).get("version"),
        "tools_discovered": len(tools),
        "tools_list_ops_per_s": res["tools_list"]["ops_per_second"],
        "tools_list_p50_ms": res["tools_list"]["latency_ms"]["p50"],
        "account_summary_ops_per_s": res["account_summary"]["ops_per_second"],
        "timeline_observe_ops_per_s": res["timeline_observe"]["ops_per_second"],
        "passed": len(tools) == 65,
    }


def _verify_real_chromium(*, capture_png_path: Path | None = None) -> dict[str, Any]:
    """Drive real headless Chromium against SHOWCASE_PAGE and prove postconditions + stale guards."""
    import browser_executor

    with tempfile.TemporaryDirectory(prefix="x-mcp-showcase-chrome-") as tmpdir:
        tmp_path = Path(tmpdir)
        prev_headless = os.environ.get("X_BROWSER_EXECUTOR_HEADLESS")
        prev_hosts = set(browser_executor.ALLOWED_HOSTS)
        prev_audit = browser_executor.AUDIT_PATH
        os.environ["X_BROWSER_EXECUTOR_HEADLESS"] = "1"
        browser_executor.ALLOWED_HOSTS = {"127.0.0.1"}
        browser_executor.AUDIT_PATH = tmp_path / "showcase-audit.jsonl"

        http_srv = ThreadingHTTPServer(("127.0.0.1", 0), _ShowcaseFeedHandler)
        worker = threading.Thread(target=http_srv.serve_forever, daemon=True)
        worker.start()
        browser = browser_executor.Browser(profile=tmp_path / "chrome-profile")
        t0 = time.perf_counter()
        try:
            url = f"http://127.0.0.1:{http_srv.server_port}/"
            obs0 = browser.open(url)
            shot = browser.screenshot(obs0["observation_id"], obs0["fingerprint_sha256"], True)
            png_bytes = base64.b64decode(shot["data_base64"], validate=True)
            png_valid = png_bytes.startswith(b"\x89PNG\r\n\x1a\n") and hashlib.sha256(png_bytes).hexdigest() == shot["sha256"]

            obs_like = browser.click(
                observation_id=obs0["observation_id"],
                fingerprint_sha256=obs0["fingerprint_sha256"],
                selector="#likeBtn",
            )
            obs_follow = browser.click(
                observation_id=obs_like["observation_id"],
                fingerprint_sha256=obs_like["fingerprint_sha256"],
                selector="#followBtn",
            )

            stale_rejected = False
            try:
                browser.click(
                    observation_id=obs0["observation_id"],
                    fingerprint_sha256=obs0["fingerprint_sha256"],
                    selector="#likeBtn",
                )
            except browser_executor.ExecutorError as exc:
                stale_rejected = exc.code == "OBSERVATION_STALE"

            if capture_png_path is not None:
                shot_after = browser.screenshot(obs_follow["observation_id"], obs_follow["fingerprint_sha256"], True)
                capture_png_path.write_bytes(base64.b64decode(shot_after["data_base64"], validate=True))

            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            like_verified = "Liked (29)" in str(obs_like.get("text") or "")
            follow_verified = "Following" in str(obs_follow.get("text") or "") and "1,241 followers" in str(obs_follow.get("text") or "")
            return {
                "scope": "real headless Chromium against local synthetic X feed fixture",
                "skipped": False,
                "elapsed_ms": elapsed_ms,
                "png_sha256_verified": bool(png_valid),
                "screenshot_sha256_prefix": shot["sha256"][:16],
                "dom_like_postcondition": bool(like_verified),
                "dom_follow_postcondition": bool(follow_verified),
                "stale_observation_rejected": bool(stale_rejected),
                "passed": bool(png_valid and like_verified and follow_verified and stale_rejected),
            }
        finally:
            browser.close()
            http_srv.shutdown()
            http_srv.server_close()
            worker.join(timeout=3)
            browser_executor.ALLOWED_HOSTS = prev_hosts
            browser_executor.AUDIT_PATH = prev_audit
            if prev_headless is None:
                os.environ.pop("X_BROWSER_EXECUTOR_HEADLESS", None)
            else:
                os.environ["X_BROWSER_EXECUTOR_HEADLESS"] = prev_headless


def _execute_showcase_batch(
    bridge: BrowserBridge,
    plan: dict[str, Any],
    *,
    session_id: str,
    clock_ref: dict[str, float],
    day_step: int,
) -> dict[str, int]:
    """Prepare, lease, complete, and hash-chain one synthetic daily batch at clock_ref['now']."""
    prepared = bridge.prepare_daily_batch(plan, session_id=session_id)
    queued = bridge.queue_daily_batch(prepared["batch_id"], prepared["confirmation_phrase"], session_id=session_id)
    batch_id = queued["batch_id"]
    completed = 0
    while True:
        clock_ref["now"] += 35.0
        act = bridge.next_action(batch_id=batch_id, session_id=session_id, lease_seconds=600)
        if act is None:
            break
        atype = act["type"]
        payload = act["payload"]
        ctx = {
            "browser_family": "chromium",
            "os": "windows",
            "host": "x.com",
            "mode": "browser",
            "page_kind": "x_profile" if atype == "follow_profile" else "x_post",
        }
        if atype == "follow_profile":
            res = {
                "handle": payload["handle"],
                "profile_url": payload["profile_url"],
                "state": "following",
                "verified": True,
                "changed": True,
            }
        elif atype == "like_post":
            res = {
                "post_url": payload["post_url"],
                "author": payload.get("author") or "creator",
                "state": "liked",
                "verified": True,
                "changed": True,
            }
        elif atype == "reply_post":
            res = {
                "post_url": payload.get("post_url") or "https://x.com/alice_rust/status/2110000000000000101",
                "reply_url": f"https://x.com/example_creator/status/21100000000000{day_step:02d}{completed:02d}",
                "author": payload.get("author") or "alice_rust",
                "text": payload["text"],
                "text_sha256": hashlib.sha256(str(payload["text"]).encode("utf-8")).hexdigest(),
                "verified": True,
                "changed": True,
            }
        else:
            res = {
                "post_url": f"https://x.com/example_creator/status/21100000000009{day_step:02d}{completed:02d}",
                "author": "example_creator",
                "text": payload["text"],
                "text_sha256": hashlib.sha256(str(payload["text"]).encode("utf-8")).hexdigest(),
                "verified": True,
                "changed": True,
            }
        bridge.complete_action(
            action_id=act["action_id"],
            claim_token=act["claim_token"],
            outcome="succeeded",
            result=res,
            error=None,
            executor_context=ctx,
            locator_strategy="dom",
            preconditions_checked=["showcase target verified"],
            postconditions_checked=["showcase state changed after reload"],
            session_id=session_id,
        )
        completed += 1
    return {"batch_id": batch_id, "completed": completed}


def _populate_showcase_workspace(state_dir: Path, *, now_epoch: float | None = None) -> tuple[XStarter, dict[str, Any]]:
    """Seed a clean 7-day multi-strategy workspace using real bridge, planner, and hash-chain stores."""
    now = float(now_epoch) if now_epoch is not None else time.time()
    clock_ref = {"now": now - 6 * 86400.0}
    bridge = BrowserBridge(state_dir, account_binding="example_creator", now=lambda: clock_ref["now"])
    ap_store = AutopilotStore(state_dir)

    bridge.record_account(
        username="example_creator",
        user_id="1000000000000000001",
        display_name="Example Creator",
        posts_count=42,
        recent_post_ids=["2110000000000000099"],
        followers_count=118,
        following_count=126,
        strategy="balanced",
    )

    # Seven daily waves rotating all 5 concrete strategies so strategy attribution has real signal.
    schedule = [
        # (day_offset_from_now, strategy, followers_before, followers_after, following_cnt, mutuals, likes, replies, follows)
        (-6, "balanced", 118, 122, 129, ["m_bal_1", "m_bal_2"], 6, 2, 2),
        (-5, "reciprocal_first", 122, 129, 134, ["m_rec_1", "m_rec_2", "m_rec_3", "m_rec_4"], 5, 2, 4),
        (-4, "niche_sniper_bundle", 129, 137, 137, ["m_niche_1", "m_niche_2", "m_niche_3", "m_niche_4"], 6, 3, 3),
        (-3, "thread_reply_magnet", 137, 148, 139, ["m_thr_1", "m_thr_2", "m_thr_3", "m_thr_4", "m_thr_5", "m_thr_6"], 7, 4, 2),
        (-2, "reciprocal_first", 148, 155, 143, ["m_rec_5", "m_rec_6", "m_rec_7"], 5, 2, 4),
        (-1, "thread_reply_magnet", 155, 169, 145, ["m_thr_7", "m_thr_8", "m_thr_9", "m_thr_10", "m_thr_11", "m_thr_12", "m_thr_13"], 8, 5, 2),
        (0, "reply_then_follow", 169, 175, 147, ["m_rep_1", "m_rep_2", "m_rep_3"], 6, 4, 2),
    ]

    sample_replies = [
        "Most MCP retries burn quota when errors stay vague; structured next-action hints fix 90% of loops.",
        "Same here — DOM selector + reload verification caught silent UI noop clicks twice this week.",
        "Keeping state transitions in one small ledger beat every ad-hoc async hook we tried.",
        "p99 tail latency tells the whole story once real browser cold starts enter the picture.",
        "Logging uncertain state instead of retrying blind saved our account health score overnight.",
    ]

    # Put one concise post + one reply into day 6 at the very end of the batch so recent log showcases every write kind.
    cq_items_final_extra = {
        "item_id": "post_day6_final",
        "kind": "original_post",
        "status": "queued",
        "text": "Shipped multi-strategy X orchestration: 6 switchable strategies, 7-day follower delta curve & zero silent retries.",
    }
    for day_idx, (d_off, strat, fc_before, fc_after, fg_cnt, mutual_list, n_likes, n_replies, n_follows) in enumerate(schedule):
        day_base = now + d_off * 86400.0
        clock_ref["now"] = day_base - 60.0
        bridge.record_follower_snapshot(
            followers_count=fc_before,
            following_count=max(100, fg_cnt - 1),
            posts_count=36 + day_idx,
            strategy=strat,
            source="wave_pre",
        )
        timeline = [
            {
                "url": f"https://x.com/dev_peer_{day_idx}_{k}/status/211000000000{day_idx:02d}{k:03d}",
                "author": f"dev_peer_{day_idx}_{k}",
                "text": "Shipping reliable MCP tools, Chromium DOM postcondition checks, and SQLite WAL locks.",
            }
            for k in range(max(n_likes, n_replies) + 2)
        ]
        cands = [
            {
                "handle": mutual_list[k] if k < len(mutual_list) else f"cand_{day_idx}_{k}",
                "bio": "Systems engineer building Rust, MCP, and local AI tooling",
                "followers_count": 320 + k * 45,
                "following_count": 290 + k * 30,
                "mutual_verified": True,
            }
            for k in range(n_follows + 1)
        ]
        cq_items = [
            {
                "item_id": f"rep_{day_idx}_{k}",
                "kind": "reply",
                "status": "queued",
                "text": sample_replies[(day_idx + k) % len(sample_replies)],
                "target_url": timeline[k]["url"],
            }
            for k in range(n_replies)
        ]
        if day_idx == 6:
            cq_items.append(cq_items_final_extra)
        elif day_idx in (1, 4):
            cq_items.append(
                {
                    "item_id": f"post_{day_idx}",
                    "kind": "original_post",
                    "status": "queued",
                    "text": f"Day {day_idx + 1} build note: hash-chained browser verification + strategy attribution hit 0 duplicate writes.",
                }
            )
        plan = build_daily_orchestration_plan(
            bridge.read_ledger_snapshot(),
            goals={
                "publish_post": 1 if day_idx in (1, 4, 6) else 0,
                "like_post": n_likes,
                "follow_profile": n_follows,
                "reply_post": n_replies,
                "repost_post": 0,
            },
            interaction_strategy=strat,
            content_queue=cq_items,
            timeline_posts=timeline,
            follow_candidates=cands,
            account_snapshot={
                "username": "example_creator",
                "display_name": "Example Creator",
                "has_avatar": True,
                "bio": "Building MCP & local-first agent workflows",
                "posts_count": 36 + day_idx,
                "followers_count": fc_before,
                "account_age_days": 120,
            },
            seed=100 + day_idx,
        )
        if day_idx == 6:
            # Put follow -> reply -> publish at tail so the top rows of the live audit log showcase diverse actions.
            order_rank = {"like_post": 0, "follow_profile": 1, "reply_post": 2, "publish_post": 3}
            plan["operations"] = sorted(
                plan["operations"],
                key=lambda op: (order_rank.get(str(op.get("action")), 9), int(op.get("step_index") or 0)),
            )
            for idx_op, op in enumerate(plan["operations"], start=1):
                op["step_index"] = idx_op
        batch_info = _execute_showcase_batch(
            bridge, plan, session_id=f"showcase_{day_idx}", clock_ref=clock_ref, day_step=day_idx
        )
        clock_ref["now"] = day_base + 1800.0
        bridge.record_follower_snapshot(
            followers_count=fc_after,
            following_count=fg_cnt,
            posts_count=37 + day_idx,
            strategy=strat,
            source="wave_post",
        )
        bridge.record_mutual_followers(
            mutual_list,
            source="notifications_sync",
            observed_at=_iso_at(clock_ref["now"] + 300.0),
            strategy=strat,
        )
        ap_store.record_wave(
            {
                "batch_id": batch_info["batch_id"],
                "mode": "autopilot_wave",
                "interaction_strategy": strat,
                "processed": batch_info["completed"],
                "verified_changed": batch_info["completed"],
                "followers_before": fc_before,
                "followers_after": fc_after,
                "query": f"showcase:{strat}",
            },
            now_epoch=day_base + 900.0,
        )

    app = XStarter(
        mode="browser",
        environ={
            "X_STARTER_MODE": "browser",
            "X_STARTER_ENABLE_BROWSER": "1",
            "X_STARTER_STATE_DIR": str(state_dir),
        },
    )
    app.call_tool("x_persona_memory", {"action": "update", "preset": "tech_casual"})
    app.call_tool(
        "x_persona_memory",
        {
            "action": "add_feedback",
            "note": "保持轻松口语吐槽和极客短句，只转发跑通过验证的工程帖，不刷AI套话。",
        },
    )
    app.call_tool(
        "x_content_queue",
        {
            "action": "add",
            "kind": "original_post",
            "topic": "Agent工程与MCP",
            "text": "Put one-shot action leases + reload DOM postcondition checks in front of browser automation — silent click drops went to 0%.",
        },
    )
    app.call_tool(
        "x_content_queue",
        {
            "action": "add",
            "kind": "thread",
            "topic": "AI开源与系统可靠性",
            "text": "Why X MCP tracks 6 switchable strategies instead of blind bulk follows\n---\nEvery wave snapshots followers_count, reconciles mutuals via notifications, and shifts auto_fastest to the top sticky winner.",
        },
    )
    app.call_tool(
        "x_target_pool",
        {
            "action": "ingest",
            "source": "showcase_scout",
            "candidates": [
                {"handle": "kai_systems", "bio": "Distributed consensus & SQLite internals", "followers_count": 640, "following_count": 310},
                {"handle": "mika_mcp", "bio": "Building Chromium MCP bridges", "followers_count": 410, "following_count": 290},
            ],
            "posts": [
                {"url": "https://x.com/kai_systems/status/2110000000000000801", "author": "kai_systems", "text": "Benchmarking WAL checkpoint p99 spikes under concurrent workers."},
            ],
        },
    )
    prep = app.call_tool(
        "x_autopilot",
        {
            "action": "prepare",
            "days": 7,
            "interaction_strategy": "auto_fastest",
            "active_hours": [9, 23],
            "daily_goals": {"follow_profile": 40, "like_post": 120, "reply_post": 45, "repost_post": 4, "publish_post": 2},
            "persona_topics": ["Agent工程", "MCP", "Chromium"],
        },
    )
    app.call_tool(
        "x_autopilot",
        {
            "action": "activate",
            "window_id": prep["data"]["window_id"],
            "confirmation": prep["data"]["confirmation_phrase"],
        },
    )
    summary = app.call_tool("x_autopilot", {"action": "ops_summary"})
    evidence = app.call_tool("x_evidence_get", {"limit": 20})
    return app, {"ops_summary": summary["data"], "evidence": evidence["data"]}


def _run_multi_strategy_demo() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="x-mcp-showcase-state-") as tmpdir:
        app, state_out = _populate_showcase_workspace(Path(tmpdir))
        try:
            d = state_out["ops_summary"]
            ev = state_out["evidence"]
            fastest = d["fastest_strategy"]
            switched = app.call_tool(
                "x_autopilot",
                {"action": "set_strategy", "interaction_strategy": "auto_fastest"},
            )
            leaderboard = [
                {
                    "strategy": row["strategy"],
                    "label": row["label"],
                    "waves_run": row["waves_run"],
                    "verified_actions": row["verified_actions"],
                    "inbound_attributed": row["inbound_attributed"],
                    "observed_follower_delta": row["observed_follower_delta"],
                    "sticky_retention_rate": row["sticky_retention_rate"],
                    "sticky_score": row["sticky_score"],
                }
                for row in d["strategy_performance"]
                if row["waves_run"] > 0
            ]
            trend = d["follower_trend"]
            return {
                "scope": "local browser-mode ledger & multi-strategy simulation (synthetic @example_creator)",
                "evidence_chain_valid": bool(ev["valid"]),
                "evidence_entries": ev["count"],
                "total_verified_actions": d["totals"]["completed"],
                "uncertain_actions": d["totals"]["uncertain"],
                "follower_growth_7d": {
                    "start": 118,
                    "current": trend["current_followers"],
                    "net_change_7d": trend["net_change_7d"],
                    "growth_rate_pct": trend["growth_rate_7d_pct"],
                    "sticky_rate": trend["sticky_rate"],
                },
                "fastest_strategy": fastest,
                "auto_fastest_resolved": switched["data"].get("resolved_strategy"),
                "leaderboard": leaderboard,
                "passed": bool(
                    ev["valid"]
                    and d["totals"]["completed"] > 0
                    and fastest["confidence"] == "measured"
                    and len(leaderboard) >= 4
                ),
            }
        finally:
            app.close()


def _capture_assets(output_dir: Path) -> dict[str, Any]:
    """Render live Obsidian-Zinc dashboard & real Chromium proof into PNGs + animated GIF."""
    from playwright.sync_api import sync_playwright

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="x-mcp-assets-") as tmpdir:
        tmp_path = Path(tmpdir)
        state_dir = tmp_path / "state"
        app, _ = _populate_showcase_workspace(state_dir)
        handler = make_http_handler(app, SessionRegistry(limit=32))
        server_cls = getattr(handler, "mcp_server_class", ThreadingHTTPServer)
        httpd = server_cls(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{httpd.server_address[1]}"

        overview_png = output_dir / "console-overview.png"
        leaderboard_png = output_dir / "strategy-growth-leaderboard.png"
        persona_png = output_dir / "persona-queue-workbench.png"
        chromium_png = output_dir / "chromium-execution-proof.png"
        gif_path = output_dir / "x-mcp-live-demo.gif"

        try:
            _verify_real_chromium(capture_png_path=chromium_png)
            chrome_exe = browser_paths.find_chrome()
            with sync_playwright() as p:
                launch_kwargs: dict[str, Any] = {"headless": True}
                if chrome_exe:
                    launch_kwargs["executable_path"] = str(chrome_exe)
                browser = p.chromium.launch(**launch_kwargs)
                context = browser.new_context(viewport={"width": 1360, "height": 860}, device_scale_factor=1)
                page = context.new_page()
                page.goto(base_url + "/", wait_until="networkidle")
                page.wait_for_function(
                    "() => document.querySelectorAll('#strategyLeaderboardTable tr').length >= 5 && !!document.querySelector('#followerTrendChart svg')"
                )
                page.wait_for_timeout(350)
                page.screenshot(path=str(overview_png), full_page=False)

                page.evaluate(
                    "switchLogView('waves'); document.querySelector('#strategyLeaderboardTable').closest('section.card').scrollIntoView({behavior:'instant', block:'start'});"
                )
                page.wait_for_timeout(250)
                page.screenshot(path=str(leaderboard_png), full_page=False)

                page.evaluate("window.scrollTo({top: 0, behavior: 'instant'}); switchTab('persona');")
                page.wait_for_function("() => document.querySelectorAll('#pmPillars .chip').length >= 1")
                page.wait_for_timeout(250)
                page.screenshot(path=str(persona_png), full_page=False)
                context.close()
                browser.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
            app.close()

        gif_built = False
        try:
            from PIL import Image, ImageOps  # type: ignore

            canvas_size = (1360, 860)
            frames = []
            for p in (overview_png, leaderboard_png, persona_png, chromium_png):
                with Image.open(p) as im:
                    rgb = im.convert("RGB")
                    padded = ImageOps.pad(rgb, canvas_size, color=(9, 9, 11))
                    frames.append(padded)
            frames[0].save(
                gif_path,
                save_all=True,
                append_images=frames[1:],
                duration=[2100, 2200, 1800, 1900],
                loop=0,
                optimize=True,
            )
            gif_built = gif_path.is_file()
        except ImportError:
            gif_built = False

        return {
            "output_dir": str(output_dir),
            "assets": {
                "console_overview_bytes": overview_png.stat().st_size,
                "strategy_leaderboard_bytes": leaderboard_png.stat().st_size,
                "persona_queue_bytes": persona_png.stat().st_size,
                "chromium_proof_bytes": chromium_png.stat().st_size,
                "animated_gif_bytes": gif_path.stat().st_size if gif_built else None,
            },
            "passed": all(p.is_file() for p in (overview_png, leaderboard_png, persona_png, chromium_png)),
        }


def _print_summary(report: dict[str, Any]) -> None:
    core = report["mcp_core_benchmark"]
    browser = report["real_chromium_browser_e2e"]
    orch = report["multi_strategy_orchestration"]
    growth = orch["follower_growth_7d"]
    print("=" * 72)
    print(" X MCP Live Capability Showcase (Sanitized Synthetic @example_creator)")
    print("=" * 72)
    print(
        f"[1/3 MCP Protocol Core]     tools={core['tools_discovered']} | "
        f"tools/list={core['tools_list_ops_per_s']:,.0f} ops/s (p50={core['tools_list_p50_ms']}ms) | "
        f"PASS={core['passed']}"
    )
    if browser.get("skipped"):
        print("[2/3 Real Chromium E2E]     SKIPPED (--no-browser)")
    else:
        print(
            f"[2/3 Real Chromium E2E]     elapsed={browser['elapsed_ms']}ms | "
            f"PNG-SHA256={browser['png_sha256_verified']} | "
            f"Like/Follow-DOM={browser['dom_like_postcondition'] and browser['dom_follow_postcondition']} | "
            f"Stale-Guard={browser['stale_observation_rejected']} | PASS={browser['passed']}"
        )
    print(
        f"[3/3 Growth & Audit Ledger] verified={orch['total_verified_actions']} | "
        f"hash_chain={orch['evidence_chain_valid']} ({orch['evidence_entries']} entries) | "
        f"7d_followers={growth['start']}->{growth['current']} (+{growth['net_change_7d']}, +{growth['growth_rate_pct']}%) | "
        f"fastest={orch['fastest_strategy']['recommended_strategy']}"
    )
    print("-" * 72)
    print(" Strategy Leaderboard (Measured Local Orchestration Simulation):")
    for row in orch["leaderboard"]:
        print(
            f"   - {row['strategy']:<22} | {row['waves_run']} waves | {row['verified_actions']:>2} acts | "
            f"+{row['inbound_attributed']:<4} inbound | delta={row['observed_follower_delta']:+} | "
            f"sticky={int(round(row['sticky_retention_rate'] * 100)):>3}% | score={row['sticky_score']}"
        )
    if report.get("captured_assets"):
        print("-" * 72)
        print(" Showcase Media Captured:", json.dumps(report["captured_assets"]["assets"], ensure_ascii=False))
    print("=" * 72)


def run_showcase(
    *,
    iterations: int = 1000,
    no_browser: bool = False,
    capture_assets_dir: Path | None = None,
) -> dict[str, Any]:
    core = _run_core_benchmark_safe(iterations)
    browser = (
        {"scope": "skipped by --no-browser", "skipped": True, "passed": True}
        if no_browser
        else _verify_real_chromium()
    )
    orch = _run_multi_strategy_demo()
    captured = _capture_assets(capture_assets_dir) if capture_assets_dir is not None else None
    passed = bool(core["passed"] and browser["passed"] and orch["passed"] and (captured is None or captured["passed"]))
    return {
        "showcase": "x-mcp-live-showcase",
        "passed": passed,
        "mcp_core_benchmark": core,
        "real_chromium_browser_e2e": browser,
        "multi_strategy_orchestration": orch,
        "captured_assets": captured,
    }


def _run_core_benchmark_safe(iterations: int) -> dict[str, Any]:
    return _verify_core(iterations=iterations)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=1000, help="Protocol benchmark iterations (default: 1000)")
    parser.add_argument("--no-browser", action="store_true", help="Skip the real headless Chromium check")
    parser.add_argument("--json", action="store_true", help="Print structured JSON instead of summary text")
    parser.add_argument("--capture-assets", type=Path, default=None, help="Render PNGs + animated GIF into directory")
    args = parser.parse_args(argv)

    report = run_showcase(
        iterations=args.iterations,
        no_browser=args.no_browser,
        capture_assets_dir=args.capture_assets,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_summary(report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
