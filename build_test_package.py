"""Build an allowlisted, credential-free internal test ZIP."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parent
FILES = (
    "server.py", "browser_bridge.py", "browser_executor.py", "browser_paths.py",
    "browser_read.py", "oauth.py", "x_api.py", "x_public.py", "write_store.py",
    "agent_help_board.py", "connector_runtime.py", "composer.py", "interaction_batch.py", "conformance_fixtures.py",
    "client_config.py", "selfcheck.py", "mcp_client.py", "tester.py", "tester.cmd", "internal-test.cmd",
    "requirements-browser.txt", "README.md", "TESTING.md", "ONE-CLICK-INTERNAL.md", "PROMPTS.md",
    "public_skill.md", "skill.md", "connector-manifest.json", "data/post_insights.json", "ui/composer.html",
    "owner_login_open.py", "owner_login_check.py", "executor_visual_e2e.py",
    "executor_matrix_e2e.py", "owner_read_e2e.py", "write_flow_e2e.py", "interaction_batch_e2e.py",
)
FORBIDDEN = frozenset({".git", ".env", "__pycache__", ".x-starter-state", ".browser-executor-profile", ".venv", ".agent-work"})


def build(output: Path) -> dict:
    if output.exists():
        raise FileExistsError("Choose a new archive path; existing releases are not overwritten.")
    payloads = {}
    for name in FILES:
        relative = Path(name)
        source = ROOT / relative
        if source.is_symlink() or not source.resolve().is_relative_to(ROOT):
            raise ValueError(f"Unsafe source: {name}")
        if set(relative.parts) & FORBIDDEN:
            raise ValueError(f"Forbidden source: {name}")
        payloads[name] = source.read_bytes()
    manifest = {
        "release": "x-starter-0.9.1-internal-test",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source": "working tree; per-file hashes are authoritative",
        "platform": "Windows, Python >=3.11, installed Chrome",
        "credentials_included": False,
        "browser_profile_included": False,
        "runtime_bundled": False,
        "dependency_install": "tester.cmd setup; network required",
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(payloads.items())},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(payloads.items()):
            archive.writestr("x-starter/" + name, data)
        archive.writestr("x-starter/PACKAGE-MANIFEST.json", json.dumps(manifest, indent=2))
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(digest + "  " + output.name + "\n", encoding="ascii")
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP integrity check failed")
        for name, expected in manifest["files"].items():
            if hashlib.sha256(archive.read("x-starter/" + name)).hexdigest() != expected:
                raise ValueError(f"Archive content mismatch: {name}")
    return {"archive": str(output), "bytes": output.stat().st_size, "sha256": digest, "files": len(payloads) + 1, "credentials_included": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    print(json.dumps(build(parser.parse_args().output.resolve()), indent=2))
