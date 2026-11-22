# X MCP

Language: English · [简体中文](README.zh-CN.md)

Give any MCP-capable Agent a structured, confirmation-gated, and verifiable way to read X, operate a personal account safely, and inspect live execution state.

- **Agent-First Execution**: Agents discover all **65** tools dynamically via `tools/list`, plan and configure workflows autonomously, and act inside bounded confirmations.
- **Observer Web Console**: A dark Obsidian-Zinc local dashboard (`ui/dashboard.html`) shows live scheduler state, pending approvals, quota telemetry, persona settings, and audit logs.
- **Verifiable & Credential-Safe**: Every write uses a server-issued confirmation phrase, one-shot action lease, target/account binding, reload postcondition verification (`uncertain` never auto-retried), and a hash-chained evidence ledger (`x_evidence_get`). Browser mode never extracts cookies, local storage, or passwords.

---

## Architecture at a Glance

1. **Public Read Path** (`x_public_profile`, `x_public_timeline`, `x_public_post`): Reads public X profiles, timelines, and canonical posts with no login and no X API key.
2. **Signed-In Browser Path** (`browser_*`, `x_owner_read`, `x_follow_batch_*`, `x_interaction_batch_*`, `x_autopilot`): Drives a dedicated persistent Chrome/Chromium profile using DOM/accessibility locators first with guarded screenshot coordinate fallback.
3. **OAuth / API Path** (`x_oauth_*`, `x_post_prepare_api`, `x_post_create_api`): Optional host-owned OAuth 2.0 PKCE flow and X API writes when configured with your own X Developer App credentials.
4. **Autonomous Orchestration & Safe Pacing**:
   - **Warmup Lifecycle (`x_account_health`)**: Evaluates standing stages (`ghost` -> `light_engage` -> `content_build` -> `growth`), circuit breakers, and remaining daily safe budget.
   - **Autopilot & Multi-Strategy Growth (`x_autopilot`, `x_daily_orchestrate_*`)**: `infer_persona` -> `prepare` (24-hour confirmation TTL, `interaction_strategy`: `auto_fastest` | `reciprocal_first` | `niche_sniper_bundle` | `thread_reply_magnet` | `reply_then_follow` | `balanced`) -> owner approves -> `activate` -> `scheduler_start` / `run_wave` / `set_strategy` / `export_report`. Coordinates `visit -> like -> contextual reply -> optional follow` author bundles, parses visible profile `followers_count` & `following_count` (`follower_snapshots`), attributes 7-day follower delta + sticky inbound conversion across strategies (`follower_trend`, `strategy_performance`, `fastest_strategy`), and automatically adapts `auto_fastest` to the highest sticky-growth strategy.
   - **Persona Memory & Sync (`x_persona_memory`)**: Supports structured templates (including the built-in `tech_casual` preset) plus throttled `sync_owner` updates (`>= 6h` between browser profile checks unless caller supplies `account_snapshot`/`own_posts`).
   - **Reply, Security & Reconciliation Guards**: Multi-language (`EN`/`ZH`/`JA`/`KO`) reply drafting strips X card headers/metrics (`clean_post_body`), skips ultra-short/ambiguous fragments fail-closed (`None`), and adapts tone to post intent (casual banter/venting, milestones, creative hobbies, language chat, or peer engineering) with zero AI boilerplate, scaling up to `45-50/day` alongside `120-150/day` likes while keeping reposts strictly curated (`<= 4/day`, `<= 1/wave`) via `score_technical_quality`. `x_notifications_read` classifies inbound events, `record_mutual_followers` reconciles mutuals without mutating hash-chained evidence, local HTTP enforces same-port `Origin` + `X-Frame-Options: DENY` + `X-Content-Type-Options: nosniff`, and conservative pacing (`75-145s` replies, `32-68s` follow micro-waves, `2h` challenge cooldown, 3-day cleanup) follows [`docs/orchestration-profile-safe.json`](docs/orchestration-profile-safe.json).

---

## Quick Start

Requirements: **Python 3.11+** (recommended `3.12`), **Google Chrome** for the signed-in browser path, and optionally **Node.js 20+** for third-party MCP protocol checks.

### 1. Launch Browser Mode + Web Console (Windows)

```powershell
dashboard.cmd
```

This starts the Streamable HTTP MCP server (`http://127.0.0.1:8765/mcp`) in browser mode using a dedicated profile directory (`.browser-main-profile` next to the script) and opens the console at `http://127.0.0.1:8765/`.

Or run directly from source in PowerShell:

```powershell
$env:X_STARTER_MODE = "browser"
$env:X_STARTER_ENABLE_BROWSER = "1"
python -I -X utf8 server.py --transport http --port 8765
```

First-time owner sign-in (human opens ordinary Chrome with the dedicated profile, signs in, then closes the window so the MCP executor can reuse it):

```powershell
$env:X_STARTER_OWNER_PROFILE = "$env:LOCALAPPDATA\x-starter\owner-profiles\owner-x"
python -I -X utf8 scripts/owner_login_open.py
python -I -X utf8 scripts/owner_login_check.py
```

### 2. Tester Workflow (Windows Setup, Smoke, Config)

```powershell
tester.cmd setup
tester.cmd smoke
tester.cmd doctor
tester.cmd login
tester.cmd check-login
tester.cmd config
```

Copy the generated entry from `mcp-client.json` or `mcp-codex.toml` into your MCP client, then hand [`skill.md`](skill.md) and [`docs/ONE-CLICK-PROMPT.md`](docs/ONE-CLICK-PROMPT.md) to your Agent.

### 3. Safe Offline Demo & Mock Server

```powershell
python -I -X utf8 server.py --demo
pwsh -NoProfile -File .\scripts\run_mock.ps1
```

Mock mode uses synthetic account `mock_creator` (`meta.simulated: true`) and never touches X or leaks credentials.

---

## Agent Workflow & Capabilities

`skill.md` defines the Agent contract: **the Agent configures and prepares everything; the owner only reviews and confirms**.

| Group | Key Tools | Purpose |
|---|---|---|
| Public Read | `x_public_profile`, `x_public_timeline`, `x_public_post` | Credential-free profile, timeline & post inspection |
| Owner / Discovery | `x_owner_read`, `x_account_get`, `x_timeline_observe`, `x_search_posts`, `x_visit_profile`, `x_notifications_read` | Signed-in reads, niche/cross-language search, profile visit before engaging, mention monitoring |
| Account Health & Persona | `x_account_health`, `x_persona_memory`, `x_target_pool`, `x_content_queue` | Standing stage checks, voice/pillar/preset memory (`tech_casual`), scout target pool, single/thread queue |
| Orchestration & Autopilot | `x_daily_orchestrate_plan`, `x_daily_orchestrate_prepare`, `x_daily_orchestrate_execute`, `x_autopilot` | Weighted daily waves, 7-day bounded autopilot scheduler, live status & CSV/JSON report export |
| Single & Batch Writes | `x_post_prepare`, `x_post_publish`, `x_post_delete`, `x_interaction_batch_*`, `x_follow_candidates_read`, `x_follow_batch_*` | Server-phrased confirmations, chunked leases, reload verification (`Following` / `unlike` / `unretweet`) |
| Browser & External Execution | `browser_open`, `browser_observe`, `browser_screenshot`, `browser_click`, `browser_type`, `browser_scroll`, `browser_close`, `x_browser_next_action`, `x_browser_complete_action`, `x_browser_action_get`, `x_evidence_get` | Built-in Playwright/Chrome execution, guarded visual fallback, external Computer-Use claim loop, hash-chained audit |
| OAuth / API | `x_oauth_start`, `x_oauth_status`, `x_oauth_disconnect`, `x_post_prepare_api`, `x_post_create_api` | Host-managed OAuth 2.0 PKCE and API posting |
| Gateway & Agent Help | `connector_list`, `connector_negotiate`, `connector_plan`, `x_compose_workbench`, `x_compose_check`, `x_interaction_link`, `agent_help_request_*`, `ahan_handoff` | Connector capability negotiation, MCP Apps composer UI, Web Intent links, and AHAN board boundary |

Discover the full catalog dynamically with `tools/list`; never hard-code schemas or confirmation phrases.

---

## First Agent Task

Start read-only without changing account state:

```text
Use xstarter MCP. First call tools/list.
Call x_public_profile for screen_name="NASA" and x_public_timeline with limit=3.
Report tool names, URLs, result counts, elapsed time, and error codes.
Do not log in or perform any writes.
```

Once connected with your signed-in browser profile, use the daily autopilot prompt in [`docs/ONE-CLICK-PROMPT.md`](docs/ONE-CLICK-PROMPT.md) or more examples in [`docs/PROMPTS.md`](docs/PROMPTS.md).

---

## Verification, Evidence & Explicit Scope

Never conflate local mock benchmarks, synthetic tests, and historical single-account browser runs with a hosted SLA.

### Run Local Verification

```powershell
python -I -X utf8 scripts/selfcheck.py
python -I -X utf8 scripts/release_check.py
python -m unittest discover -s tests -p "test_*.py"
```

Optional HTTP read check against a running server (`http://127.0.0.1:8765/mcp`):

```powershell
python -I -X utf8 e2e/public_e2e.py --handle NASA
```

### Historical Recorded Benchmarks & Limits

| Check | Recorded Result | Exact Scope & Boundary |
|---|---|---|
| MCP Conformance (`2026-10-22`) | `39/39` | Official profile against local server |
| `checkmcp` release scan | `94/94` | Schema, error, and security scan in recorded run |
| Local HTTP Mock Load | `994.06 RPS` (`512` clients) / `533.08 RPS` (`2048` clients), `0` errors | Local mock mode only; no X network or model calls |
| Bounded External Agent/X Batch | `31` actions / `133 s` (`20` likes, `1` repost, `10` replies) | Single owner-controlled acceptance run; not a 1-minute SLA |
| External Agent Follow Audit | `202` unique verified follows across `20` runs | Window-bounded real runs; `100` `uncertain` rows stopped without retry |
| Visual / Viewport Fixtures | `27/27` visual fallback, `42/42` matrix | Real Chromium against local synthetic fixtures |

Important safety lessons from our [`2026-11-14` suspension postmortem](docs/reports/POSTMORTEM-SUSPENSION-2026-11-14.md):
- Aggressive machine-paced bursts or continuing writes after X refusal codes (`283`, `161`, `141`, `37`) lead to enforcement.
- All autopilot and orchestrator defaults now enforce human-like randomized delays, circuit breakers, 2h security cooldowns, and strict per-action limits.
- Self-hosted browser mode consumes **0 X API credits**, while external X API mode uses X Developer Console per-request billing plus your own model/compute costs.

See [`docs/RELEASE_READINESS.md`](docs/RELEASE_READINESS.md) and [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md) for current release blockers (including selecting a `LICENSE` before public distribution).

---

## Repository Structure

```text
x-starter/
├── server.py                     MCP server entrypoint (stdio + Streamable HTTP) & web console host
├── dashboard.cmd / tester.cmd    Windows one-click dashboard & test-harness launchers
├── skill.md / public_skill.md    Agent operating contract & public connector skill
├── connector-manifest.json       Connector capabilities & schema manifest
├── requirements-browser.txt      Playwright browser dependencies
├── src/                          Core runtime modules
│   ├── account_orchestrator.py   Health, warmup, persona memory, target pool, content queue & daily plans
│   ├── autopilot_ops.py          Background scheduler, persona sync, follow/reply waves & reports
│   ├── daily_action_budget.py    Per-action daily caps & rate-gate enforcement
│   ├── browser_bridge.py         Confirmation phrases, one-shot leases, evidence ledger & hash chain
│   ├── browser_executor.py       Chromium DOM/a11y/visual executor
│   ├── browser_read.py           Visible profile, timeline, search, notification & candidate readers
│   ├── follow_batch.py / follow_orchestration.py / interaction_batch.py
│   ├── oauth.py / x_api.py / x_public.py / write_store.py / ledger_lock.py
│   └── agent_help_board.py / connector_runtime.py / composer.py / client_config.py
├── ui/
│   ├── dashboard.html            Obsidian-Zinc observer & lightweight approval console
│   └── composer.html             MCP Apps interactive post composer workbench
├── scripts/                      Setup, doctor, owner login, release check, benchmark, loadtest & mock runner
├── e2e/                          Acceptance, contract matrix, visual/matrix & loopback E2E suites
├── tools/                        CLI utilities (safe pacing, window/calibration permits, lease cleanup, audits)
├── examples/                     Codex, Claude Desktop, OpenClaw & raw HTTP setup examples
├── tests/                        Unit & offline contract test suite
└── docs/                         Guides, prompts, release readiness, reports & promotion assets
    ├── ONE-CLICK-PROMPT.md       Daily autopilot quickstart prompt
    ├── PROMPTS.md                Prompt catalog
    ├── TESTING.md                Windows setup & verification runbook
    ├── RELEASE_READINESS.md      Release boundaries & blockers
    ├── PERFORMANCE.md            Benchmark snapshot & cost boundaries
    ├── PRODUCT-REPORT.md         Product overview
    ├── API-ADAPTER-ROADMAP.md    Dual-path API + browser architecture roadmap
    ├── orchestration-profile-safe.json
    ├── reports/                  Historical incident & verification logs (Nov 2026)
    └── promotion/                Demo runbooks & promo assets
```

---

## Contributing & License

- Contribution rules & security reporting: [`CONTRIBUTING.md`](CONTRIBUTING.md), [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md), [`SECURITY.md`](SECURITY.md).
- License status: No open-source `LICENSE` has been committed yet; see [`docs/LICENSE-DECISION.md`](docs/LICENSE-DECISION.md) before redistributing.
- Repository: <https://github.com/Guoz7/x-mcp>
