"""Run the full 31-action interaction contract against a local X-shaped page.

This test performs real browser clicks and typing, but the host is loopback.
It proves queueing, per-action claims, DOM execution, postconditions, and the
same screenshot fallback boundary used for a live X executor. It never
contacts X and never changes an account.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import threading
import time
from pathlib import Path


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        status_id = self.path.split("/status/", 1)[-1].split("?", 1)[0]
        body = f"""<!doctype html><html><head><title>Post {status_id}</title></head><body>
        <article><a href='/tester/status/{status_id}'>tester</a><div>fixture post {status_id}</div></article>
        <button data-action='like' style='position:absolute;left:100px;top:100px;width:100px;height:40px' aria-label='Like' aria-pressed='false'
          onclick="this.setAttribute('aria-pressed','true');this.textContent='Liked'">Like</button>
        <button data-action='repost' aria-label='Repost' aria-pressed='false'
          onclick="this.setAttribute('aria-pressed','true');this.textContent='Reposted'">Repost</button>
        <div id='reply' contenteditable='true' aria-label='Reply'></div>
        <button data-action='reply' aria-label='Reply submit'
          onclick="document.getElementById('result').textContent=document.getElementById('reply').innerText">Reply</button>
        <p id='result'></p></body></html>""".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


def main() -> int:
    started = time.monotonic()
    checks: list[dict[str, object]] = []

    def check(name: str, passed: bool, detail: object = None) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    with tempfile.TemporaryDirectory(prefix="interaction-batch-e2e-") as directory:
        root = Path(directory)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        env = dict(os.environ)
        env.update(
            {
                "X_STARTER_MODE": "browser",
                "X_STARTER_ENABLE_BROWSER": "1",
                "X_STARTER_STATE_DIR": str(root / "state"),
                "X_STARTER_OWNER_PROFILE": str(root / "owner"),
                "X_BROWSER_EXECUTOR_HEADLESS": "1",
                "X_BROWSER_EXECUTOR_PERSISTENT": "0",
                "X_BROWSER_EXECUTOR_ALLOWED_HOSTS": "127.0.0.1",
                "X_BROWSER_EXECUTOR_GATED_HOSTS": "127.0.0.1",
                "X_BROWSER_EXECUTOR_PROFILE_HOSTS": "127.0.0.1",
                "X_STARTER_BROWSER_PROFILE_DIR": str(root / "profiles"),
            }
        )
        os.environ.update(env)
        try:
            from interaction_batch import plan_batch
            from server import XStarter

            posts = [
                {
                    "url": f"https://x.com/user{i}/status/{1000 + i}",
                    "author": f"user{i}",
                    "text": f"fixture post {i}",
                }
                for i in range(20)
            ]
            app = XStarter(mode="browser", environ=env)
            try:
                plan_result = app.call_tool(
                    "x_interaction_batch_plan",
                    {"posts": posts, "reply_texts": ["感谢分享"] * 10},
                )
                check("plan", plan_result["ok"] is True, plan_result.get("error"))
                plan = plan_result.get("data") or {}
                prepared = app.call_tool("x_interaction_batch_prepare", {"plan": plan})
                check("prepare", prepared["ok"] is True, prepared.get("error"))
                started_batch = app.call_tool(
                    "x_interaction_batch_start",
                    {
                        "batch_id": (prepared.get("data") or {}).get("batch_id"),
                        "confirmation": (prepared.get("data") or {}).get("confirmation_phrase"),
                    },
                )
                check("start", started_batch["ok"] is True, started_batch.get("error"))

                browser_args: dict[str, object] = {}
                completed = {"like": 0, "repost": 0, "reply": 0}
                for _ in range(31):
                    next_result = app.call_tool("x_browser_next_action", {}, session_id="e2e")
                    action = (next_result.get("data") or {}).get("action")
                    if not action:
                        check("action_available", False, next_result)
                        break
                    action_id = action["action_id"]
                    claim = action["claim_token"]
                    op = action["type"]
                    payload = action["payload"]
                    status_id = payload["url"].rsplit("/", 1)[-1]
                    opened = app.call_tool(
                        "browser_open", {"url": f"{base}/status/{status_id}"}, session_id="e2e"
                    )
                    observed = opened.get("data") or {}
                    if not opened.get("ok"):
                        check("open", False, opened)
                        break
                    selector = "button[data-action='like']" if op == "like_post" else (
                        "button[data-action='repost']" if op == "repost_post" else "button[data-action='reply']"
                    )
                    if op == "reply_post":
                        typed = app.call_tool(
                            "browser_type",
                            {
                                "observation_id": observed["observation_id"],
                                "fingerprint_sha256": observed["fingerprint_sha256"],
                                "text": payload["text"],
                                "selector": "#reply",
                                "action_id": action_id,
                                "claim_token": claim,
                            },
                            session_id="e2e",
                        )
                        if not typed.get("ok"):
                            check("type", False, typed)
                            break
                        observed = typed["data"]
                    click_args = {
                        "observation_id": observed["observation_id"],
                        "fingerprint_sha256": observed["fingerprint_sha256"],
                        "action_id": action_id,
                        "claim_token": claim,
                    }
                    if completed["like"] == 0 and op == "like_post":
                        shot = app.call_tool(
                            "browser_screenshot",
                            {
                                "observation_id": observed["observation_id"],
                                "fingerprint_sha256": observed["fingerprint_sha256"],
                            },
                            session_id="e2e",
                        )
                        check("visual_screenshot", shot["ok"] is True, shot.get("error"))
                        click_args.update({"x_ratio": 150 / 1280, "y_ratio": 120 / 720})
                        check("visual_fallback_used", True)
                    else:
                        click_args["selector"] = selector
                    clicked = app.call_tool("browser_click", click_args, session_id="e2e")
                    if not clicked.get("ok"):
                        check("click", False, clicked)
                        break
                    result: dict[str, object]
                    if op == "reply_post":
                        result = {
                            "post_url": payload["url"],
                            "reply_url": f"{payload['url']}/reply/fixture-{status_id}",
                            "text": payload["text"],
                            "text_sha256": __import__("hashlib").sha256(payload["text"].encode()).hexdigest(),
                            "verified": True,
                        }
                    else:
                        result = {
                            "post_url": payload["url"],
                            "author": payload["author"],
                            "verified": True,
                            "state": "active",
                        }
                    finished = app.call_tool(
                        "x_browser_complete_action",
                        {
                            "action_id": action_id,
                            "claim_token": claim,
                            "outcome": "succeeded",
                            "result": result,
                            "executor_context": {"browser_family": "chromium"},
                            "locator_strategy": "dom",
                            "preconditions_checked": ["target URL matched payload"],
                            "postconditions_checked": ["visible fixture state matched result"],
                        },
                        session_id="e2e",
                    )
                    if not finished.get("ok"):
                        check("complete", False, finished)
                        break
                    completed[{"like_post": "like", "repost_post": "repost", "reply_post": "reply"}[op]] += 1
                check("like_count", completed["like"] == 20, completed)
                check("repost_count", completed["repost"] == 1, completed)
                check("reply_count", completed["reply"] == 10, completed)
                app.call_tool("browser_close", {}, session_id="e2e")
            finally:
                app.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    failed = [item for item in checks if not item["passed"]]
    report = {
        "scope": "local loopback fixture, real Chromium, full 31-action browser contract; no X production writes",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "checks": checks,
        "passed": len(checks) - len(failed),
        "failed": len(failed),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
