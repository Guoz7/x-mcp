"""Real-browser compatibility matrix for the executor.

Each case starts a fresh Chromium context, because Playwright fixes viewport,
device scale factor, locale and color scheme for the lifetime of a context.
The page is a synthetic local fixture; no X, no owner profile, no network.

Scope: proves the executor renders and clicks at the configured geometry,
locale and color scheme. It does not prove X's own layout at those settings.
"""

from __future__ import annotations

import json
import os
import random
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


PAGE = b'''<!doctype html><html><head><title>Matrix fixture</title>
<style>
  body {margin:0;height:2200px;background:#fff;color:#000;font:16px sans-serif}
  #target {position:absolute;background:#1d4ed8;color:#fff;border:0;font:16px sans-serif}
  html.dark body {background:#0b0b0b;color:#f5f5f5}
</style></head><body>
<div id="size"></div>
<button id="target">target</button>
<script>
  document.getElementById('size').textContent =
    innerWidth + 'x' + innerHeight + '@' + devicePixelRatio + ' ' + navigator.language;
  if (matchMedia('(prefers-color-scheme: dark)').matches) {
    document.documentElement.classList.add('dark');
  }
  // Random target placement per page load, so a hardcoded coordinate cannot pass.
  const w = 140, h = 60;
  const x = Math.round(Math.random() * (innerWidth - w - 40)) + 20;
  const y = Math.round(Math.random() * (innerHeight - h - 40)) + 20;
  const node = document.getElementById('target');
  node.style.left = x + 'px';
  node.style.top = y + 'px';
  node.style.width = w + 'px';
  node.style.height = h + 'px';
  window.__target = {x: x + w / 2, y: y + h / 2};
  node.onclick = () => { document.getElementById('size').textContent = 'clicked'; };
</script></body></html>'''


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args: object) -> None:
        return


CASES = [
    {
        "name": "desktop-1280-dpr1-light-en",
        "width": 1280, "height": 720, "dpr": 1.0,
        "locale": "en-US", "scheme": "light", "expect_class": "desktop",
    },
    {
        "name": "mobile-390-dpr3-dark-zh",
        "width": 390, "height": 844, "dpr": 3.0,
        "locale": "zh-CN", "scheme": "dark", "expect_class": "mobile",
    },
    {
        "name": "tablet-820-dpr2-light-ja",
        "width": 820, "height": 1180, "dpr": 2.0,
        "locale": "ja-JP", "scheme": "light", "expect_class": "tablet",
    },
    {
        "name": "desktop-1920-dpr1.5-dark-de",
        "width": 1920, "height": 1080, "dpr": 1.5,
        "locale": "de-DE", "scheme": "dark", "expect_class": "desktop",
    },
    {
        "name": "mobile-360-dpr2-light-ar",
        "width": 360, "height": 640, "dpr": 2.0,
        "locale": "ar-SA", "scheme": "light", "expect_class": "mobile",
    },
    {
        "name": "desktop-1440-dpr1-default-fr",
        "width": 1440, "height": 900, "dpr": 1.0,
        "locale": "fr-FR", "scheme": "no-preference", "expect_class": "desktop",
    },
]


def _configure(case: dict, temporary: str, index: int) -> None:
    os.environ["X_BROWSER_EXECUTOR_WIDTH"] = str(case["width"])
    os.environ["X_BROWSER_EXECUTOR_HEIGHT"] = str(case["height"])
    os.environ["X_BROWSER_EXECUTOR_DEVICE_SCALE_FACTOR"] = str(case["dpr"])
    os.environ["X_BROWSER_EXECUTOR_LOCALE"] = case["locale"]
    os.environ["X_BROWSER_EXECUTOR_COLOR_SCHEME"] = case["scheme"]
    os.environ["X_BROWSER_EXECUTOR_PROFILE"] = str(
        Path(temporary) / f"profile-{index}"
    )


def _png_size(png: bytes) -> tuple[int, int]:
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AssertionError("not a PNG")
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def main() -> int:
    checks: list[dict] = []
    random.seed()
    with tempfile.TemporaryDirectory(prefix="executor-matrix-") as temporary:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_port}/"

        os.environ["X_BROWSER_EXECUTOR_HEADLESS"] = "1"
        os.environ["X_BROWSER_EXECUTOR_ALLOWED_HOSTS"] = "127.0.0.1"
        os.environ["X_BROWSER_EXECUTOR_PERSISTENT"] = "0"
        os.environ["X_BROWSER_EXECUTOR_START_TIMEOUT"] = "90"

        from browser_executor import Browser

        def check(case: str, name: str, condition: bool, detail: object = None) -> None:
            entry = {"case": case, "name": name, "passed": bool(condition)}
            if detail is not None:
                entry["detail"] = detail
            checks.append(entry)

        try:
            for index, case in enumerate(CASES):
                name = case["name"]
                _configure(case, temporary, index)
                browser = Browser(profile=Path(os.environ["X_BROWSER_EXECUTOR_PROFILE"]))
                try:
                    observation = browser.open(url)
                    check(name, "viewport_css_pixels",
                          observation["width"] == case["width"]
                          and observation["height"] == case["height"],
                          [observation["width"], observation["height"]])
                    check(name, "device_pixel_ratio",
                          abs(observation["dpr"] - case["dpr"]) < 0.01,
                          observation["dpr"])
                    check(name, "locale_forwarded",
                          observation["locale"] == case["locale"],
                          observation["locale"])
                    expected_scheme = (
                        "light" if case["scheme"] == "no-preference" else case["scheme"]
                    )
                    check(name, "color_scheme_forwarded",
                          observation["color_scheme"] == expected_scheme,
                          observation["color_scheme"])
                    check(name, "viewport_class",
                          observation["viewport_class"] == case["expect_class"],
                          observation["viewport_class"])

                    shot = browser.screenshot(
                        observation["observation_id"],
                        observation["fingerprint_sha256"],
                        True,
                    )
                    from base64 import b64decode

                    width_px, height_px = _png_size(b64decode(shot["data_base64"]))
                    check(name, "screenshot_is_scaled",
                          width_px == round(case["width"] * case["dpr"])
                          and height_px == round(case["height"] * case["dpr"]),
                          [width_px, height_px])

                    center = browser._run_on_worker(
                        lambda: browser.page.evaluate("window.__target")
                    )
                    after = browser.click(
                        observation_id=observation["observation_id"],
                        fingerprint_sha256=observation["fingerprint_sha256"],
                        x_ratio=center["x"] / case["width"],
                        y_ratio=center["y"] / case["height"],
                    )
                    check(name, "coordinate_click_hits_target_at_this_dpr",
                          "clicked" in after["text"],
                          {"target": center, "text": after["text"][:40]})
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    passed = sum(item["passed"] for item in checks)
    failed = [item for item in checks if not item["passed"]]
    report = {
        "scope": (
            "real headless Chromium, synthetic local page; "
            "proves executor geometry/locale/scheme handling, not X layout"
        ),
        "cases": len(CASES),
        "checks": checks,
        "passed": passed,
        "failed": len(failed),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
