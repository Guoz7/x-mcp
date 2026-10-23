"""End-to-end acceptance for the confirmed write path.

Runs the real MCP tools against a synthetic, loopback X-shaped site so the
whole gate is exercised without touching the owner account:

    x_post_prepare -> human phrase -> x_post_publish -> claim ->
    browser_type -> browser_click -> x_browser_complete_action

The page records the submitted text. That gives an independent postcondition:
the text on the page must equal the prepared payload, or the run fails.

Scope: proves the gate, the staged claim, and the postcondition check. It does
not prove X's production composer, and it never contacts X.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


PAGE = b'''<!doctype html><html><head><title>Compose / X</title></head><body>
<nav><a href="/home">Home</a><a href="/tester">Profile</a></nav>
<main>
<article><a href="/tester/status/900"><time>1m</time></a>
  <div>tester</div><div>existing post</div></article>
</main>
<div contenteditable="true" id="box" style="width:400px;height:80px;border:1px solid #ccc"></div>
<button id="submit" onclick="
  document.getElementById('result').textContent =
    'PUBLISHED ' + document.getElementById('box').innerText;
">Post</button>
<p id="result"></p>
</body></html>'''

PROFILE = b'''<!doctype html><html><head><title>tester (@tester) / X</title>
<meta property="og:image" content="https://pbs.twimg.com/profile_banners/4242424242/1/600x200">
</head><body><nav><a href="/home">Home</a><a href="/tester">Profile</a></nav>
<main><div>tester</div><div>@tester</div><div>3 posts</div>
<article><a href="/tester/status/900"><time>1m</time></a><div>existing post</div></article>
</main></body></html>'''


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        body = PROFILE if path == "/tester" else PAGE
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


def main() -> int:
    checks: list[dict] = []

    def check(name: str, condition: bool, detail: object = None) -> None:
        checks.append({"name": name, "passed": bool(condition), "detail": detail})

    with tempfile.TemporaryDirectory(prefix="write-flow-e2e-") as temporary:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        owner = Path(temporary) / "owner"
        owner.mkdir()

        os.environ.update(
            {
                "X_STARTER_MODE": "browser",
                "X_STARTER_ENABLE_BROWSER": "1",
                "X_STARTER_STATE_DIR": str(Path(temporary) / "state"),
                "X_STARTER_READ_BASE_URL": base,
                "X_STARTER_OWNER_PROFILE": str(owner),
                "X_BROWSER_EXECUTOR_HEADLESS": "1",
                "X_BROWSER_EXECUTOR_ALLOWED_HOSTS": "127.0.0.1",
                "X_BROWSER_EXECUTOR_PROFILE_HOSTS": "127.0.0.1",
                # Exercise the real X write gate against the fixture, so the
                # test cannot pass while the gate is silently not applied.
                "X_BROWSER_EXECUTOR_GATED_HOSTS": "127.0.0.1",
                "X_BROWSER_EXECUTOR_PERSISTENT": "0",
                "X_BROWSER_EXECUTOR_PROFILE": str(Path(temporary) / "profile"),
                "X_STARTER_BROWSER_PROFILE_DIR": str(Path(temporary) / "profiles"),
            }
        )
        from server import XStarter

        app = XStarter(mode="browser", environ=os.environ)
        try:
            # 1. Observe the account so a write may be prepared.
            observed = app.call_tool("x_owner_read", {"target": "account"})
            check("account_observed", observed["ok"] is True, observed.get("error"))

            text = "hello from the confirmed write test"
            prepared = app.call_tool("x_post_prepare", {"text": text, "ttl_seconds": 300})
            check("write_prepared", prepared["ok"] is True, prepared.get("error"))
            record = prepared.get("data") or {}
            phrase = record.get("confirmation_phrase", "")
            check("confirmation_phrase_issued", phrase.startswith("PUBLISH "), phrase)

            # 2. A wrong phrase must not queue anything.
            wrong = app.call_tool(
                "x_post_publish",
                {"write_id": record.get("write_id", ""), "confirmation": "PUBLISH 000000"},
            )
            check("wrong_phrase_rejected", wrong["ok"] is False)
            check(
                "wrong_phrase_error",
                wrong.get("error", {}).get("code") == "CONFIRMATION_MISMATCH",
                wrong.get("error", {}).get("code"),
            )

            # 3. The right phrase queues exactly one action.
            queued = app.call_tool(
                "x_post_publish",
                {"write_id": record.get("write_id", ""), "confirmation": phrase},
            )
            check("write_queued", queued["ok"] is True, queued.get("error"))
            action = ((queued.get("data") or {}).get("action") or {})
            check("queued_action_is_publish", action.get("type") == "publish_post", action.get("type"))

            # 4. The payload text must match the prepared hash.
            payload = action.get("payload") or {}
            check("payload_matches_text", payload.get("text") == text)

            # 5. Claim it, then type and click with the same claim.
            claimed = app.call_tool("x_browser_next_action", {})
            live = ((claimed.get("data") or {}).get("action") or {})
            check("action_claimed", claimed["ok"] is True and bool(live.get("claim_token")))
            grant = {
                "action_id": live.get("action_id", ""),
                "claim_token": live.get("claim_token", ""),
            }

            opened = app.call_tool("browser_open", {"url": f"{base}/home"})
            check("composer_page_opened", opened["ok"] is True, opened.get("error"))
            observed_page = opened["data"]

            typed = app.call_tool(
                "browser_type",
                {
                    "observation_id": observed_page["observation_id"],
                    "fingerprint_sha256": observed_page["fingerprint_sha256"],
                    "text": text,
                    "selector": "#box",
                    **grant,
                },
            )
            check("payload_typed", typed["ok"] is True, typed.get("error"))

            after_type = typed["data"]
            clicked = app.call_tool(
                "browser_click",
                {
                    "observation_id": after_type["observation_id"],
                    "fingerprint_sha256": after_type["fingerprint_sha256"],
                    "selector": "#submit",
                    **grant,
                },
            )
            check("submit_clicked", clicked["ok"] is True, clicked.get("error"))

            # 6. Independent postcondition: the page must show the payload.
            visible = (clicked.get("data") or {}).get("text", "")
            check(
                "page_shows_the_payload",
                f"PUBLISHED {text}" in visible,
                visible[-80:] if visible else None,
            )

            # 7. A further click with the same claim must be refused.
            stale = app.call_tool(
                "browser_click",
                {
                    "observation_id": (clicked.get("data") or {}).get("observation_id", ""),
                    "fingerprint_sha256": (clicked.get("data") or {}).get("fingerprint_sha256", ""),
                    "selector": "#submit",
                    **grant,
                },
            )
            check("claim_is_single_use", stale["ok"] is False)

            # 8. The honest result is accepted and stored.
            completed = app.call_tool(
                "x_browser_complete_action",
                {
                    "action_id": grant["action_id"],
                    "claim_token": grant["claim_token"],
                    "outcome": "succeeded",
                    "result": {
                        "post_url": "https://x.com/tester/status/901",
                        "author": "tester",
                        "text": text,
                        "text_sha256": payload.get("text_sha256", ""),
                    },
                    "executor_context": {"browser_family": "chromium"},
                    "locator_strategy": "dom",
                    "postconditions_checked": ["page shows the payload"],
                },
            )
            check("publish_completed", completed["ok"] is True, completed.get("error"))
            check(
                "write_record_completed",
                (completed.get("data") or {}).get("status") == "completed",
                (completed.get("data") or {}).get("status"),
            )

            # 9. A completed action cannot report a different account later.
            reused = app.call_tool(
                "x_browser_complete_action",
                {
                    "action_id": grant["action_id"],
                    "claim_token": grant["claim_token"],
                    "outcome": "succeeded",
                    "result": {
                        "post_url": "https://x.com/someoneelse/status/123",
                        "author": "someoneelse",
                        "text": text,
                        "text_sha256": payload.get("text_sha256", ""),
                    },
                    "executor_context": {"browser_family": "chromium"},
                },
            )
            check("completed_action_cannot_be_rewritten", reused["ok"] is False)
            check(
                "reuse_is_rejected_as_unclaimed",
                reused.get("error", {}).get("code") == "ACTION_NOT_CLAIMED",
                reused.get("error", {}).get("code"),
            )

            evidence = app.call_tool("x_evidence_get", {"limit": 100})["data"]
            check("evidence_chain_valid", evidence["valid"] is True)
            kinds = [
                entry.get("kind") or entry.get("type")
                for entry in evidence.get("entries", [])
            ]
            check(
                "write_admission_is_evidenced",
                any("write_queued" in str(item) for item in kinds),
                kinds,
            )
            check(
                "publish_completion_is_evidenced",
                any("completed" in str(item) for item in kinds),
                kinds,
            )
        finally:
            app.close()
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    failed = [item for item in checks if not item["passed"]]
    report = {
        "scope": "synthetic X-shaped loopback site; proves the gate and postcondition, not X",
        "checks": checks,
        "passed": len(checks) - len(failed),
        "failed": len(failed),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
