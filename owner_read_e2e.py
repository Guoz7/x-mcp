"""End-to-end acceptance for the owner read loop.

Serves an X-shaped synthetic site on loopback and runs the real MCP tool:

    x_owner_read -> queue -> claim -> browser_read -> bridge validation

Two cases must both hold:

1. A login wall fails closed with ``OWNER_SIGNED_OUT`` and a recorded action.
2. A signed-in-shaped page produces a validated, hash-chained snapshot.

Scope: proves the loop, the schema checks, and the failure behaviour against
real Chromium. It does not prove X's production markup, and it never touches
the owner profile.

Run with a Python environment that has Playwright and a local Chrome:

    python -X utf8 owner_read_e2e.py
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


HOME = b'''<!doctype html><html><head><title>Home / X</title></head><body>
<nav><a href="/home">Home</a><a href="/explore">Explore</a>
<a href="/tester">Profile</a></nav>
<main>
<article><a href="/alice/status/111"><time>1h</time></a>
  <div>alice</div><div>first visible post</div></article>
<article><a href="/bob/status/222"><time>2h</time></a>
  <div>bob</div><div>second visible post</div></article>
</main></body></html>'''

PROFILE = b'''<!doctype html><html><head><title>tester (@tester) / X</title>
<meta property="og:image" content="https://pbs.twimg.com/profile_banners/4242424242/1700000000/600x200">
</head><body>
<nav><a href="/home">Home</a><a href="/tester">Profile</a></nav>
<main><div>tester</div><div>@tester</div><div>7 posts</div>
<article><a href="/tester/status/333"><time>3h</time></a>
  <div>my own recent post</div></article>
</main></body></html>'''

WALL = b'''<!doctype html><html><head><title>X</title></head><body>
<div>Sign in</div><a href="/i/flow/login">Log in</a></body></html>'''


class Fixture(BaseHTTPRequestHandler):
    signed_out = False

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if Fixture.signed_out or path.startswith("/i/"):
            body = WALL
        elif path == "/tester":
            body = PROFILE
        else:
            body = HOME
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

    with tempfile.TemporaryDirectory(prefix="owner-read-e2e-") as temporary:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"

        os.environ.update(
            {
                "X_STARTER_MODE": "browser",
                "X_STARTER_ENABLE_BROWSER": "1",
                "X_STARTER_STATE_DIR": str(Path(temporary) / "state"),
                "X_STARTER_READ_BASE_URL": base,
                "X_BROWSER_EXECUTOR_HEADLESS": "1",
                "X_BROWSER_EXECUTOR_ALLOWED_HOSTS": "127.0.0.1",
                "X_BROWSER_EXECUTOR_PROFILE_HOSTS": "127.0.0.1",
                "X_BROWSER_EXECUTOR_PERSISTENT": "0",
                "X_BROWSER_EXECUTOR_PROFILE": str(Path(temporary) / "profile"),
                "X_STARTER_BROWSER_PROFILE_DIR": str(Path(temporary) / "profiles"),
            }
        )
        from server import XStarter

        try:
            app = XStarter(mode="browser", environ=os.environ)
            try:
                Fixture.signed_out = True
                failed = app.call_tool("x_owner_read", {"target": "timeline"})
                check("login_wall_fails_closed", failed["ok"] is False, failed.get("error"))
                check(
                    "login_wall_error_code",
                    failed.get("error", {}).get("code") == "OWNER_SIGNED_OUT",
                    failed.get("error", {}).get("code"),
                )
                check(
                    "login_wall_records_action",
                    "action_id" in failed.get("error", {}).get("details", {}),
                )
                check(
                    "login_wall_reports_external",
                    failed["meta"]["real_external_calls"] is True,
                )
                check("login_wall_not_simulated", failed["meta"]["simulated"] is False)

                Fixture.signed_out = False
                timeline = app.call_tool("x_owner_read", {"target": "timeline", "limit": 2})
                # The fixture is not x.com, and the bridge only accepts
                # canonical X permalinks. A loopback URL must be rejected:
                # this proves the validator is not relaxed for convenience.
                check(
                    "non_x_permalink_is_rejected",
                    timeline["ok"] is False
                    and timeline["error"]["code"] == "INVALID_ACTION_RESULT",
                    timeline.get("error", {}).get("code"),
                )
                check(
                    "rejected_timeline_is_recorded",
                    "action_id" in timeline.get("error", {}).get("details", {}),
                )

                account = app.call_tool("x_owner_read", {"target": "account"})
                check("account_read_ok", account["ok"] is True, account.get("error"))
                snapshot = (account.get("data") or {}).get("snapshot", {})
                check("account_has_numeric_id", str(snapshot.get("user_id", "")).isdigit())
                check("account_has_username", snapshot.get("username") == "tester")
                context = json.dumps((account.get("data") or {}).get("executor_context", {})).lower()
                check(
                    "executor_context_has_no_secrets",
                    all(word not in context for word in ("cookie", "token", "password")),
                )

                evidence = app.call_tool("x_evidence_get", {"limit": 50})["data"]
                check("evidence_chain_valid", evidence["valid"] is True)
                check("evidence_has_all_actions", evidence["count"] >= 6, evidence["count"])

                # Canonical success path: the bridge accepts real x.com
                # permalinks. Feed it the same shape a signed-in read produces,
                # without inventing a network call.
                from browser_bridge import BrowserBridge

                bridge = BrowserBridge(str(Path(temporary) / "canonical-state"))
                queued = bridge.queue_observation(action_type="observe_timeline")
                claimed = bridge.next_action(lease_seconds=60)
                completed = bridge.complete_action(
                    action_id=claimed["action_id"],
                    claim_token=claimed["claim_token"],
                    outcome="succeeded",
                    result={
                        "posts": [
                            {
                                "url": "https://x.com/alice/status/111",
                                "author": "alice",
                                "text": "first visible post",
                            }
                        ]
                    },
                    error=None,
                    executor_context={"browser_family": "chromium"},
                )
                snapshot = completed.get("result") or {}
                check("canonical_timeline_accepted", completed["status"] == "completed")
                check(
                    "canonical_timeline_is_hashed",
                    bool(snapshot.get("snapshot_sha256")),
                    snapshot.get("hash_basis"),
                )
                check(
                    "canonical_timeline_keeps_text_hash",
                    bool(snapshot["posts"][0].get("text_sha256")),
                )
            finally:
                app.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    failed = [item for item in checks if not item["passed"]]
    report = {
        "scope": "synthetic X-shaped fixture in real Chromium; not X production markup",
        "checks": checks,
        "passed": len(checks) - len(failed),
        "failed": len(failed),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
