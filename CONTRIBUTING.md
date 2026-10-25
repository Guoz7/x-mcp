# Contributing

Thank you for improving X Starter. Keep changes small, testable, and explicit
about which execution mode they affect.

## Before you change code

1. Read `README.md`, `TESTING.md`, and `RELEASE_READINESS.md`.
2. Open an issue for behavior changes that affect the MCP contract, browser
   permissions, or credential handling.
3. Never add real X credentials, cookies, browser profiles, private logs, or
   account screenshots to a branch or issue.

## Local checks

Use Python 3.11 or newer:

```powershell
python release_check.py
python -m unittest discover -s tests -p "test_*.py"
git diff --check
```

For browser changes, install the optional dependency with `tester.cmd setup`
and follow the gated steps in `TESTING.md`. A mock test is not evidence of a
real X or DSH run.

## Pull requests

- State the execution mode and user-visible behavior that changed.
- Include the exact commands, exit codes, and relevant result files.
- Separate unit, protocol, browser-fixture, DSH, and real-X evidence.
- Add a regression test for every fixed defect.
- Update `README.md`, `public_skill.md`, the manifest, or release notes when
  the public contract changes.
- Do not mix formatting-only changes with security or authorization changes.

## Security-sensitive changes

Changes involving OAuth, browser profiles, action claims, confirmation phrases,
tenant isolation, or secret storage need a focused review. Report suspected
credential exposure privately as described in `SECURITY.md`.
