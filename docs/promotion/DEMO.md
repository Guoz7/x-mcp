# Promotion demo runbook

This runbook creates a reproducible demo with the synthetic test account
`mock_creator`. It is safe to record because it does not connect to X.

## Instant real-Chromium showcase (recommended)

```powershell
python -X utf8 scripts/showcase.py
```

This runs the 65-tool core benchmark, real headless Chromium DOM postcondition checks against a local synthetic feed, and a 7-day multi-strategy growth + SHA-256 hash-chain simulation in ~10 seconds without touching real X.

## One-minute local mock demo

```powershell
python -I -X utf8 server.py --demo > demo-result.json
```

The output shows four steps:

1. Connect and inspect the test account.
2. Draft one post.
3. Queue it for review.
4. Read illustrative metrics and prepare an AHAN handoff.

The output must say:

```text
ok=true
mode=mock
tool_count=65
username=mock_creator
disclosure=The entire walkthrough is simulated. No X or AHAN request was made.
```

## Record the screen

Record the terminal and the generated narrated deck. Keep the following label
visible in the first frame:

> 本地合成测试账号 · 不连接真实 X · 数据用于演示 MCP 调用链

Do not record a personal browser profile for the first public video. A real
account video needs a separate owner-approved test account and redaction review.

## Optional tester smoke

For the Windows package demo:

```powershell
tester.cmd setup
tester.cmd smoke
```

This checks a real MCP stdio process in mock mode. It does not access X.

## Promotion acceptance

Before publishing the video, check:

- The video says `mock` or `synthetic` wherever the test account appears.
- Every performance number includes its scope.
- No account name, cookie, password, token, private message or browser profile appears.
- The GitHub link points to the source repository.
- The post links to `docs/PROMOTION.md` and `docs/RELEASE_READINESS.md`.
