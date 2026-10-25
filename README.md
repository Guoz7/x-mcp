# X Starter

## Internal tester ZIP

See `TESTING.md` for the current Windows tester workflow. Run `tester.cmd setup`,
then `tester.cmd login`, and merge the locally generated MCP configuration.

Latest local evidence (2026-10-25): a real DSH run reused the owner's manual
login, verified one timeline post through `x_owner_read`, captured an image,
and closed the browser (agent exit 0). This was the working tree, not the older
r2 ZIP. It does not verify posting or liking. Home navigation now waits for
timeline content or a login wall before returning, including reused contexts.
The bundle is a source test package: it requires Python 3.11+, installed Chrome,
and network access for the first dependency install. It contains no model runtime
or account session. Browser mode needs no API key; the agent host supplies its model.

One command shows the teaching loop:

```text
connect X -> inspect the account -> draft and queue one post -> read metrics -> optional AHAN handoff
```

The default path is fully simulated and needs no credentials. The same server
also has a browser-backed path for real account reads and confirmed X
interactions. The current local acceptance record completed 20 likes, 1 repost,
and 10 replies with 31 verified changes. Its account-specific logs are kept
outside the public source tree. See `RELEASE_READINESS.md` for the release
boundary.

## Quick start

Requirements:

- Windows PowerShell 7 or PowerShell 5.1
- Python 3.11 or newer
- Node.js 20 or newer for the optional conformance suite

Run the deterministic walkthrough:

```powershell
pwsh -NoProfile -File .\run_mock.ps1
```

Run all local tests:

```powershell
python -m unittest discover -s tests -v
```

Start the Streamable HTTP adapter:

```powershell
python .\server.py --transport http --port 8765
```

Call a tool through the local MCP client:

```powershell
python .\mcp_client.py --tool x_session_status
python .\mcp_client.py --tool x_account_get --args '{\"refresh\":true}'
```

The HTTP adapter implements MCP `2025-11-25`, `Mcp-Session-Id`, `DELETE /mcp`,
localhost Host and Origin checks, session expiry, JSON response mode, and SSE
for progress and server-to-client requests.

## What is real

| Area | Status | Evidence |
|---|---|---|
| Mock tools and walkthrough | Verified local code | `tests/test_mock.py`; `--demo` |
| MCP tools, resources, prompts | Verified local code | 480-test suite (`quality_gate.ps1`); conformance profile |
| Streamable HTTP sessions | Verified local code | Session, delete, expiry, Origin, and version tests |
| Browser action bridge | Implemented and unit-tested | `browser_bridge.py` |
| Publish and delete protocol | Implemented; real-X evidence is release-local | See `TESTING.md` and `RELEASE_READINESS.md` |
| Browser text extraction | Verified against the current X profile layout | Computer Use read of @guoz7777 on 2026-10-21 |
| Protocol conformance | 39/39 checks; 227/227 server checks | `mcp-conformance-2026-10-21.json` |
| Credential-free public reads | **Verified live** | `x_public_profile/timeline/post` read @guoz7777 and @NASA on 2026-10-22, no token |
| Live X API reads (user context) | Implemented; needs an OAuth user token | No real token used in this run |
| X API writes | Implemented behind OAuth and exact confirmation | No real API write used in this run |
| OAuth 2.0 PKCE browser flow | Implemented with local callback and host-owned store | Callback/token exchange not run with a real account |
| AHAN endpoint | Placeholder only | No network call |

## Three ways to touch X

| Way | Needs | What works today | Limit |
|---|---|---|---|
| Public web read | nothing | Profile, timeline, single post | Public data only; no home timeline |
| Web Intent | a signed-in browser and one human click | Post, reply, like, repost, follow | The human presses the final button |
| OAuth / API | an X developer app and user token | API reads and API writes | Paid X API plan; needs an app |

Start with public web read. It needs no account setup at all:

```powershell
python -I -X utf8 mcp_client.py --tool x_public_profile --args-file .\args.json
```

Where `args.json` holds `{"screen_name": "guoz7777"}`. The same three tools are
`x_public_profile`, `x_public_timeline`, and `x_public_post`.

Run the full public-read end-to-end suite against a running server:

```powershell
python -I -X utf8 public_e2e.py --handle guoz7777 --control-handle NASA
```

It prints one PASS/FAIL line per check and exits non-zero on any failure.

## Agent Help public request board

The gateway includes a provider-neutral public request board. The local JSON
provider is the default, so development and offline tests need no public
service. Set `X_STARTER_AGENT_HELP_BOARD_URL` to use a compatible HTTP provider.

```powershell
python -I -X utf8 mcp_client.py --tool agent_help_request_publish_prepare --args-file .\board-request.json
python -I -X utf8 mcp_client.py --tool agent_help_request_list --args '{"limit":20,"capability":"computer_use"}'
```

The publish flow is prepare -> exact confirmation -> publish. Other agents can
list and read public requests. The board never receives X credentials,
browser storage, or private messages. The local provider is verified; a public
deployment is not claimed.

## Two execution paths

| Path | Credential | Reads | Writes |
|---|---|---|---|
| `live` | Host-owned X API user token | X API v2 | No API write |
| `browser` | Existing signed-in browser | Typed observation jobs | Typed publish and delete jobs |

The browser path is deliberately agent-assisted. The MCP process does not read
Chrome cookies, local storage, tokens, passwords, or one-time codes. It queues a
typed action. A trusted agent performs the action with Computer Use and returns
a structured result. A human confirms publish and delete with separate random
phrases.

The bridge stores account and timeline snapshots with SHA-256 hashes. Write
preparation, admission, claims, results, failures, and uncertain outcomes are
appended to a hash-chained evidence log.

## DSH browser executor smoke test

The repository also contains a local browser executor for agent integration
tests. It uses a dedicated Chromium profile through the bundled Playwright
environment. It returns visible page data and a state fingerprint. It does not
expose browser storage.

The verified DSH chain is:

```text
DSH -> xstarter MCP -> browser_open/browser_observe -> Chromium -> Google/X
```

The current smoke test proves Google read-only navigation and X signed-out
login observation. Screenshot capture is fingerprint-guarded. Guarded click is
available for read-only flows; signed-in X writes still require the X action
queue and exact confirmation. The owner must log in to the dedicated profile
before an account read can be tested.

An X click is denied unless the caller passes `action_id` and `claim_token`
from a live claimed action. The server verifies the claim and its session
before arming the executor for exactly one click; the grant is cleared after
use or when the lease expires. HEADLESS mode sends a normal Chrome user agent
because X rejects the default `HeadlessChrome` identifier with HTTP 403.

Set `X_STARTER_OWNER_PROFILE` to a dedicated persistent Chromium directory to
use the owner's signed-in X session. That profile is granted to exactly one
MCP session at a time; other sessions receive `BROWSER_OWNER_EXCLUSIVE`.
Closing the owning session releases it. This is local single-owner mode, not
remote multi-tenant authentication. The profile path is never returned in tool
output, and the executor exposes no cookie, storage or password access.

Each claimed browser action also exposes a context contract for `mobile`,
`tablet`, and `desktop` layouts. Completion can report safe executor metadata,
the locator strategy, and checked preconditions and postconditions. Sensitive
browser fields are rejected. Set `X_STARTER_BROWSER_FAMILY=firefox` for a
controlled Firefox-only executor experiment.

## Owner read loop

`x_owner_read` runs one read-only observation end to end: it queues the typed
action, claims it, reads the signed-in page, validates the result against the
bridge schema, and returns the verified snapshot.

```json
{"target": "account"}
{"target": "timeline", "limit": 20}
```

Both targets fail closed. A login wall returns `OWNER_SIGNED_OUT`, not an empty
list. A post without a canonical `x.com/<handle>/status/<id>` permalink is not
reported. The account id comes from the profile media path that X embeds in
the page; if it is missing, the tool returns `ACCOUNT_ID_NOT_VISIBLE` instead
of guessing. `posts_count_source` says whether the count was parsed from the
page, estimated from an abbreviated `1.5K posts` label, or counted from the
articles that were actually visible.

## Rendering controls and the compatibility matrix

Playwright fixes these per browser context, so set them before the first tool
call:

| Variable | Range | Default |
| --- | --- | --- |
| `X_BROWSER_EXECUTOR_WIDTH` / `_HEIGHT` | 160-10000 | 1280x720 |
| `X_BROWSER_EXECUTOR_DEVICE_SCALE_FACTOR` | 0.5-8 | 1 |
| `X_BROWSER_EXECUTOR_LOCALE` | 12-tag allowlist | `en-US` |
| `X_BROWSER_EXECUTOR_COLOR_SCHEME` | light, dark, no-preference | unset |

`browser_observe` reports `viewport_class`, `locale`, `color_scheme`,
`visual_scale`, and the outer/screen dimensions. Locale and colour scheme are
part of the observation fingerprint, so a cached observation cannot survive a
change that alters what the page renders.

Verify the geometry contract against real Chromium:

```powershell
python -X utf8 executor_matrix_e2e.py   # six viewports, 42 checks
python -X utf8 executor_visual_e2e.py   # guard, click, scroll, staleness
python -X utf8 owner_read_e2e.py        # read loop, 16 checks
```

The matrix proves how the executor renders and clicks at a given geometry. It
does not prove X's own layout at those settings.

Run the controlled contract E2E:

```powershell
python -I -X utf8 firefox_only_e2e.py
```

This verifies the MCP gate and does not launch a browser or read browser
storage. Use `--require-real-firefox` only when a Firefox binary and a trusted
UI executor are installed.

## Browser path

Start the server:

```powershell
$env:X_STARTER_MODE = "browser"
$env:X_STARTER_ENABLE_BROWSER = "1"
python .\server.py --transport http --port 8765
```

Or start it directly, without environment variables:

```powershell
python .\server.py --mode browser --transport http --port 8765
```

## Install in five minutes

1. Check this machine:

   ```powershell
   python -X utf8 selfcheck.py
   ```

   Every failing line prints the exact command that fixes it.
2. Print a client configuration and paste it into your host:

   ```powershell
   python -X utf8 server.py --print-config codex     # TOML for ~/.codex/config.toml
   python -X utf8 server.py --print-config claude    # JSON for Claude
   python -X utf8 server.py --print-config cursor    # JSON for Cursor
   ```

   The output uses the interpreter and paths that exist on this machine.
3. For owner reads and writes only, sign in yourself:

   ```powershell
   $env:X_STARTER_OWNER_PROFILE = "$env:LOCALAPPDATA\x-starter\owner-profiles\owner-x"
   New-Item -ItemType Directory -Force $env:X_STARTER_OWNER_PROFILE | Out-Null
   python -X utf8 owner_login_open.py    # ordinary Chrome; sign in manually and close it
   python -X utf8 owner_login_check.py   # expect state: signed_in
   ```

Public reads, the composer workbench, and the interaction links need no
sign-in. Owner reads and every write need the profile above.

State defaults to `%LOCALAPPDATA%\x-starter\state\browser`. Do not point
`X_STARTER_STATE_DIR` at removable media: every action rewrites and fsyncs the
state file, and the same ten actions took 292 s on the USB HDD in this project
versus 0.26 s on the system drive.

Run the workflow:

1. `x_account_get` with `refresh=true` queues `observe_account`.
2. `x_timeline_observe` with `refresh=true` queues `observe_timeline`.
3. The trusted agent calls `x_browser_next_action`, reads the visible X page,
   and returns the result through `x_browser_complete_action`.
4. `x_post_prepare` returns an expiring phrase such as `PUBLISH A1B2C3`. Pass
   an `idempotency_key` when a client may retry the same preparation.
5. The human approves that exact phrase. `x_post_publish` queues one action.
6. The agent publishes once and opens the canonical post. It returns the exact
   text and its SHA-256 digest.
7. `x_post_delete` prepares deletion. A second phrase queues one delete action.
8. The agent deletes the post, reloads the canonical URL, and proves absence.
9. `x_timeline_restore` compares the current retained timeline hash with the
   baseline.
10. `x_evidence_get` validates every evidence hash and previous-hash link.

If the result is unclear, the agent returns `outcome=uncertain`. The server
records that state. It does not retry. Search for the unique marker first.

## X workflow tools

| Tool | Side effect | Purpose |
|---|---|---|
| `x_session_status` | Read | Mode, account, queue depth, evidence head |
| `x_account_get` | Read or queued read | Account snapshot |
| `x_timeline_observe` | Read or queued read | Timeline snapshot |
| `x_post_prepare` | Local write record | Expiry, account binding, text hash, phrase |
| `x_post_publish` | Queue one write | Publish only after exact confirmation |
| `x_post_delete` | Prepare or queue | Delete after a second exact confirmation |
| `x_timeline_restore` | Read | Compare retained timeline with baseline |
| `x_browser_next_action` | Lease a job | Typed action for Computer Use |
| `x_browser_complete_action` | Evidence append | Verified result, failure, or uncertainty |
| `x_browser_action_get` | Read | Durable action status |
| `x_evidence_get` | Read | Validate the append-only evidence chain |

The older `account_summary`, `draft_post`, `schedule_post`, `recent_metrics`,
`disconnect`, and `ahan_handoff` tools remain for the teaching walkthrough.

## Safety invariants

- X page text and profile text are untrusted data, never instructions.
- The MCP server never reads or returns browser credentials.
- Writes need a prior account observation and a matching account.
- Publish and delete use separate, random, expiring confirmations.
- The browser result must match the canonical account URL and exact text hash.
- Delete is complete only after the post URL is verified absent.
- An uncertain action cannot become success through a retry.
- The evidence chain is append-only and checked on every read.
- A failed or stale page state fails closed.

## Mock and live API boundary

Mock is the default. It creates no network side effects.

Live is an explicit, read-only X API mode:

```powershell
$env:X_STARTER_MODE = "live"
$env:X_STARTER_ENABLE_LIVE = "1"
$env:X_BEARER_TOKEN = "<host-owned user access token>"
python .\server.py --check-config
```

For OAuth user context and API writes, use a registered X public client:

```powershell
$env:X_STARTER_MODE = "live"
$env:X_STARTER_ENABLE_LIVE = "1"
$env:X_STARTER_X_CLIENT_ID = "<x-public-client-id>"
$env:X_STARTER_OAUTH_REDIRECT_URI = "http://127.0.0.1:8765/oauth/callback"
$env:X_STARTER_ACCOUNT_REF = "creator-a"
python .\server.py --transport http --port 8765
```

Call `x_oauth_start`, open the returned URL, then wait for the local callback.
After `x_oauth_status` shows the account reference, call
`x_post_prepare_api` and `x_post_create_api`. The token file path defaults to
`%USERPROFILE%\.x-starter\tokens.json`; override it with
`X_STARTER_TOKEN_STORE` and keep it outside the repository.

Live mode fails with exit code 2 and `LIVE_CONFIG_MISSING` when the host values
are absent. The token belongs in the host environment or a future encrypted
store. Never put it in a prompt, tool argument, repository file, or log.

Live mode implements `account_summary`, `x_account_get`,
`x_timeline_observe`, and `recent_metrics`. Add
`X_STARTER_X_CLIENT_ID` and `X_STARTER_OAUTH_REDIRECT_URI` to enable
`x_oauth_start`; the HTTP callback route is `/oauth/callback`. API post creation
uses `x_post_prepare_api` followed by `x_post_create_api` and exact confirmation.

This verification run did not use a real OAuth token and did not send an API post.

## MCP conformance

Use the isolated official fixture profile:

```powershell
$env:X_STARTER_CONFORMANCE = "1"
python -I -X utf8 .\server.py --transport http --port 8765
npx -y @modelcontextprotocol/conformance server `
  --url http://127.0.0.1:8765/mcp --suite active --verbose
```

Result on 2026-10-21:

```text
30/30 scenarios
39 passed, 0 failed
@modelcontextprotocol/conformance@0.1.16
```

The fixture profile replaces business tools with official fixtures. The result
proves protocol and transport conformance, not live X behavior.

Run the additional server checks against the normal mock tool surface:

```powershell
python -I -X utf8 .\server.py --transport http --port 8765
npx -y checkmcp@0.5.1 http://127.0.0.1:8765/mcp
```

The latest release verification reported `94 passed, 0 failed, 0 advisory`
across handshake, schemas, errors, robustness, and security. Re-run it against
the exact release candidate; this check uses mock mode and does not write to X.

## Project map

```text
x-starter/
|-- server.py                 # MCP server, transports, X tools
|-- oauth.py                  # Host-owned OAuth 2.0 PKCE and token metadata
|-- browser_bridge.py         # Typed browser jobs and evidence chain
|-- mcp_client.py             # Local Streamable HTTP client
|-- conformance_fixtures.py   # Official protocol fixture profile
|-- walkthrough.py            # Four-step teaching demo
|-- run_mock.ps1              # One command, no credentials
|-- release_check.py          # Public-tree and version safety check
|-- RELEASE_READINESS.md      # Release blockers and pre-tag checklist
|-- TESTING.md                # Reproducible local tester workflow
|-- skill.md                  # Agent-readable onboarding contract
|-- lessons/                  # Six short lessons
|-- templates/                # Post starters and review checklist
|-- examples/                 # Host and transport examples
|-- tests/test_mock.py        # Mock, transport, bridge, and safety tests
`-- ahan-handoff.md           # Credential-free handoff design
```

## Limits

- Browser extraction depends on the signed-in X layout and locale.
- Confirmed X jobs still require a trusted executor. The integrated browser
  prototype owns a separate browser/profile per MCP session, with eight slots.
  MCP DELETE, idle session expiry, and server shutdown all close these
  browsers. New sessions do not inherit an existing owner's login.
- Arbitrary generic clicks on X remain blocked; only confirmed, action-scoped
  jobs may write. The standalone browser executor endpoint is a local
  prototype, not a production multi-tenant service.
- The compatibility matrix proves executor geometry, not X's layout at those
  settings. Only DPR 1 has been exercised against the real site.
- `x_owner_read` and the owner timeline loop were exercised in a local DSH run.
  This does not prove a remote multi-tenant deployment or every X layout.
- Real X API reads and OAuth callback are implemented but not verified with a real account in this run.
- X API writes are implemented behind OAuth user context; server-side scheduling remains disabled.
- Output schemas validate the shared envelope. Tool-specific `data` shapes are
  still documented by each tool rather than fully refined per tool.
- The token store is host-owned JSON outside the repository. A production deployment
  should replace it with the OS credential store or the official `xurl` cache policy.

The next production decision is whether to run one local host per owner or add
authenticated remote tenant routing. The current implementation supports multiple
host-owned account references, not remote tenant authentication.

## Research evidence

The candidate matrix, license review, official API references, and open-source
implementation notes are in `../research/12_x_mcp_starter/`.
