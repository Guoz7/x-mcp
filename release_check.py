"""Check that the repository is safe to prepare for a public release.

This is a lightweight source-tree check. It does not prove deployment safety,
OAuth behavior, or real X behavior. It deliberately reports missing license
approval separately so a pre-release checkout can still be tested in CI.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parent
FORBIDDEN_NAMES = {
    ".env",
    ".env.local",
    ".x-starter-state",
    ".x-starter-token-store",
    ".browser-executor-profile",
    ".agent-work",
    "mcp-client.json",
    "mcp-codex.toml",
    "control-test-result.html",
}
PRIVATE_PATH_RE = re.compile(
    r"(?i)(?:C:[\\/]Users[\\/]54736(?:[\\/]|$)|"
    r"D:[\\/]workspace[\\/]agent-help(?:[\\/]|$)|E:[\\/]app(?:[\\/]|$))"
)
SECRET_RE = re.compile(
    r"(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|"
    r"AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{20,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"Bearer\s+[A-Za-z0-9._-]{24,})"
)


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    names = [item for item in result.stdout.decode().split("\0") if item]
    return [ROOT / name for name in names]


def _version() -> str:
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    match = re.search(r'^SERVER_VERSION\s*=\s*["\']([^"\']+)', source, re.MULTILINE)
    if not match:
        raise ValueError("server.py does not declare SERVER_VERSION")
    return match.group(1)


def run(*, require_license: bool = False) -> tuple[bool, list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    required = (
        "README.md",
        "public_skill.md",
        "skill.md",
        "connector-manifest.json",
        "TESTING.md",
        "RELEASE_READINESS.md",
        "SECURITY.md",
        "CONTRIBUTING.md",
        ".github/workflows/ci.yml",
    )
    for name in required:
        if not (ROOT / name).is_file():
            errors.append(f"missing required release file: {name}")

    try:
        manifest = json.loads((ROOT / "connector-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        errors.append(f"connector-manifest.json is not valid JSON: {exc}")
        manifest = {}

    try:
        version = _version()
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
        version = ""
    for label, value in (
        ("manifest service.version", manifest.get("service", {}).get("version")),
        ("manifest mcp.server_version", manifest.get("mcp", {}).get("server_version")),
    ):
        if version and value != version:
            errors.append(f"{label}={value!r} does not match SERVER_VERSION={version!r}")

    try:
        tracked = _tracked_files()
    except (OSError, subprocess.CalledProcessError) as exc:
        errors.append(f"cannot enumerate tracked files: {exc}")
        tracked = []

    for path in tracked:
        relative = path.relative_to(ROOT).as_posix()
        if any(part in FORBIDDEN_NAMES for part in Path(relative).parts) or path.name in FORBIDDEN_NAMES:
            errors.append(f"forbidden release file is tracked: {relative}")
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            errors.append(f"cannot read tracked file {relative}: {exc}")
            continue
        if b"\x00" in data:
            continue
        text = data.decode("utf-8", errors="replace")
        if PRIVATE_PATH_RE.search(text):
            errors.append(f"private machine path found in tracked file: {relative}")
        if SECRET_RE.search(text):
            errors.append(f"credential-shaped value found in tracked file: {relative}")

    if not (ROOT / "LICENSE").is_file():
        message = "LICENSE is missing; choose a license before public publication"
        (errors if require_license else warnings).append(message)

    if "YOUR_AHAN_HOST" in (ROOT / "connector-manifest.json").read_text(encoding="utf-8"):
        warnings.append("connector endpoint is still a placeholder; do not claim hosted availability")

    return not errors, errors, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-license", action="store_true")
    args = parser.parse_args(argv)
    passed, errors, warnings = run(require_license=args.require_license)
    for warning in warnings:
        print(f"WARN: {warning}")
    for error in errors:
        print(f"ERROR: {error}")
    print(f"release_check: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
