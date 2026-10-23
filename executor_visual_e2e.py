"""Real-browser executor checks against a local, explicitly synthetic page.

This is not DSH model evaluation and does not contact X or use an owner profile.
Run with a Python environment containing Playwright and a local Chrome binary.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


PAGE = b'''<!doctype html><html><head><title>Executor fixture</title></head>
<style>html {scroll-behavior:smooth}</style><body style="margin:0;height:2400px">
<button id="target" style="position:absolute;left:100px;top:100px;width:200px;height:80px"
 onclick="document.getElementById('result').textContent='clicked'">Local test</button>
<p id="result" style="position:absolute;top:200px">waiting</p>
<a>duplicate</a><a>duplicate</a></body></html>'''


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args: object) -> None:
        pass


def main() -> int:
    checks: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="executor-e2e-") as temporary:
        os.environ["X_BROWSER_EXECUTOR_PROFILE"] = str(Path(temporary) / "profile")
        os.environ["X_BROWSER_EXECUTOR_HEADLESS"] = "1"
        os.environ["X_BROWSER_EXECUTOR_ALLOWED_HOSTS"] = "127.0.0.1"
        from browser_executor import Browser, ExecutorError

        browser = Browser()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def check(name: str, condition: bool) -> None:
            checks.append({"name": name, "passed": bool(condition)})
            if not condition:
                raise AssertionError(name)

        def rejected(name: str, operation: object, code: str) -> None:
            try:
                operation()
            except ExecutorError as error:
                check(name, error.code == code)
            else:
                check(name, False)

        try:
            url = f"http://127.0.0.1:{server.server_port}/"
            observation = browser.open(url)
            check("real_page_title", observation["title"] == "Executor fixture")
            image = browser.screenshot(
                observation["observation_id"], observation["fingerprint_sha256"], True
            )
            png = base64.b64decode(image["data_base64"], validate=True)
            check("png_integrity", png.startswith(b"\x89PNG\r\n\x1a\n") and
                  hashlib.sha256(png).hexdigest() == image["sha256"])
            after = browser.click(
                observation_id=observation["observation_id"],
                fingerprint_sha256=observation["fingerprint_sha256"],
                x_ratio=200 / observation["width"], y_ratio=140 / observation["height"],
            )
            check("coordinate_click_postcondition", "clicked" in after["text"])
            scrolled = browser.scroll(observation_id=after["observation_id"],
                                      fingerprint_sha256=after["fingerprint_sha256"], delta_y=500)
            check("bounded_scroll_postcondition", scrolled["scrollY"] == 500)
            restored = browser.scroll(observation_id=scrolled["observation_id"],
                                      fingerprint_sha256=scrolled["fingerprint_sha256"], delta_y=-500)
            check("bounded_scroll_restore", restored["scrollY"] == 0)
            rejected("used_observation_rejected", lambda: browser.click(
                observation_id=observation["observation_id"],
                fingerprint_sha256=observation["fingerprint_sha256"], selector="#target"
            ), "OBSERVATION_STALE")
            fresh = browser.observe()
            rejected("ambiguous_selector_rejected", lambda: browser.click(
                observation_id=fresh["observation_id"],
                fingerprint_sha256=fresh["fingerprint_sha256"], selector="a"
            ), "TARGET_NOT_UNIQUE")
            fresh = browser.observe()
            browser._run_on_worker(lambda: browser.page.evaluate("window.scrollTo({top:500,behavior:'instant'})"))
            rejected("live_scroll_invalidates_coordinates", lambda: browser.click(
                observation_id=fresh["observation_id"],
                fingerprint_sha256=fresh["fingerprint_sha256"], x_ratio=0.2, y_ratio=0.2
            ), "OBSERVATION_STALE")
            browser._run_on_worker(lambda: browser.page.evaluate("window.scrollTo({top:0,behavior:'instant'})"))
            fresh = browser.observe()
            browser.screenshot(fresh["observation_id"], fresh["fingerprint_sha256"])
            browser._run_on_worker(lambda: browser.page.evaluate(
                "document.getElementById('target').style.left='600px'"
            ))
            rejected("layout_shift_invalidates_coordinates", lambda: browser.click(
                observation_id=fresh["observation_id"],
                fingerprint_sha256=fresh["fingerprint_sha256"], x_ratio=0.2, y_ratio=0.2
            ), "OBSERVATION_STALE")
        finally:
            browser.close()
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
    report = {"scope": "real headless Chromium, synthetic local page; not DSH or X",
              "checks": checks, "passed": sum(item["passed"] for item in checks),
              "failed": sum(not item["passed"] for item in checks)}
    print(json.dumps(report, indent=2))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
