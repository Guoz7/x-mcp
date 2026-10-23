"""One command that reproduces the whole acceptance chain.

Runs every gate in order and prints one verdict. Nothing is mocked into
looking real: each stage says what it proves and what it does not.

    python -X utf8 acceptance.py            # full run
    python -X utf8 acceptance.py --quick    # skip the slow real-browser stages

Stages:

1. environment   - selfcheck: interpreter, Playwright, Chrome, storage
2. unit          - the offline contract and unit suite
3. protocol      - MCP conformance and checkmcp against a live server
4. executor      - real Chromium: guard, click, scroll, staleness
5. matrix        - real Chromium: six viewports, DPR, locale, scheme
6. owner read    - X-shaped fixture: account and timeline loop, fail-closed
7. write         - X-shaped fixture: publish and delete through the real gate
8. dsh           - real DSH agent against Google and X (opt-in)

Exit code 0 only when every stage that ran passed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
RUNTIME = os.environ.get("X_STARTER_PYTHON", "").strip() or sys.executable


def _run(
    command: list[str], *, timeout: int = 600, shell: bool = False
) -> tuple[int, str]:
    try:
        finished = subprocess.run(
            command,
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
            # npx is a .cmd shim on Windows and needs the shell to resolve.
            shell=shell,
        )
    except subprocess.TimeoutExpired:
        return 124, "stage timed out"
    except OSError as exc:
        return 127, f"could not start stage: {exc}"
    return finished.returncode, (finished.stdout or "") + (finished.stderr or "")


def _protocol(port: int) -> tuple[int, str]:
    """Run the official suites against a temporary conformance server."""

    env = os.environ.copy()
    env.update({"X_STARTER_CONFORMANCE": "1", "X_STARTER_MODE": "mock"})
    server = subprocess.Popen(
        [RUNTIME, "-I", "-X", "utf8", "server.py", "--transport", "http", "--port", str(port)],
        cwd=SCRIPT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    import http.client
    import time

    try:
        deadline = time.monotonic() + 30
        ready = False
        while time.monotonic() < deadline:
            if server.poll() is not None:
                return 1, "conformance server exited early"
            try:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                connection.request("GET", "/health")
                ready = connection.getresponse().status == 200
                connection.close()
            except OSError:
                ready = False
            if ready:
                break
            time.sleep(0.3)
        if not ready:
            return 1, "conformance server did not become ready"
        url = f"http://127.0.0.1:{port}/mcp"
        code, official = _run(
            ["npx", "-y", "@modelcontextprotocol/conformance", "server",
             "--url", url, "--suite", "active"],
            timeout=900,
            shell=True,
        )
        if code != 0:
            return code, official
        code, strict = _run(
            ["npx", "-y", "checkmcp@0.5.1", url], timeout=900, shell=True
        )
        return code, official + "\n" + strict
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def _tail(text: str, limit: int = 400) -> str:
    stripped = text.strip()
    return stripped[-limit:] if len(stripped) > limit else stripped


def _json_verdict(text: str) -> dict | None:
    """Read the last JSON object a stage printed."""

    for start in range(len(text) - 1, -1, -1):
        if text[start] != "{":
            continue
        try:
            parsed = json.loads(text[start:])
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reproduce the X MCP acceptance chain")
    parser.add_argument("--quick", action="store_true", help="skip real-browser and DSH stages")
    parser.add_argument("--with-dsh", action="store_true", help="also run the real DSH agent stages")
    args = parser.parse_args(argv)

    stages: list[dict] = []

    def record(name: str, proves: str, code: int, output: str) -> None:
        entry = {"stage": name, "proves": proves, "exit": code, "passed": code == 0}
        verdict = _json_verdict(output)
        if verdict:
            for key in ("passed", "failed", "ready_for_public_reads", "ready_for_owner_account"):
                if key in verdict:
                    entry[key] = verdict[key]
        if code != 0:
            entry["output"] = _tail(output)
        stages.append(entry)

    code, out = _run([RUNTIME, "-X", "utf8", "selfcheck.py"], timeout=120)
    record("environment", "interpreter, Playwright, Chrome, storage", code, out)

    code, out = _run(
        [RUNTIME, "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
        timeout=900,
    )
    record("unit", "offline contracts and gates", code, out)

    if not args.quick:
        code, out = _protocol(8771)
        record("protocol", "official conformance and checkmcp", code, out)

    if not args.quick:
        for script, proves, timeout in (
            ("executor_visual_e2e.py", "guard, click, scroll, staleness in real Chromium", 600),
            ("executor_matrix_e2e.py", "six viewports, DPR, locale, colour scheme", 900),
            ("owner_read_e2e.py", "owner read loop fails closed and validates", 600),
            ("write_flow_e2e.py", "publish and delete through the real write gate", 600),
        ):
            code, out = _run([RUNTIME, "-X", "utf8", script], timeout=timeout)
            record(script.removesuffix(".py"), proves, code, out)

    if args.with_dsh:
        probe = Path(r"D:\workspace\agent-help\.agent-work\dsh_native_image_probe.py")
        if not probe.is_file():
            record("dsh", "real DSH agent against Google and X", 127, "probe script missing")
        else:
            for site in ("google", "x"):
                env_site = os.environ.get("DSH_PROBE_READ_SITE")
                os.environ["DSH_PROBE_READ_SITE"] = site
                os.environ.pop("DSH_PROBE_VISUAL_CLICK", None)
                code, out = _run(
                    [RUNTIME, "-X", "utf8", str(probe)],
                    timeout=600,
                )
                if env_site is None:
                    os.environ.pop("DSH_PROBE_READ_SITE", None)
                else:
                    os.environ["DSH_PROBE_READ_SITE"] = env_site
                record(f"dsh-{site}", f"real DSH read-only run on {site}", code, out)

    failed = [item for item in stages if not item["passed"]]
    report = {
        "verdict": "pass" if not failed else "fail",
        "stages": stages,
        "passed": len(stages) - len(failed),
        "failed": len(failed),
        "not_proven": [
            "owner-authenticated X timeline read (profile reports signed_out)",
            "any real X write (excluded until the owner signs in and confirms)",
            "X's own layout at non-default DPR (matrix proves the executor only)",
        ],
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
