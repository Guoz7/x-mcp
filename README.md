# X Starter

One command shows the teaching loop:

```text
connect X -> inspect the account -> draft and queue one post -> read metrics -> optional AHAN handoff
```

The default path is fully simulated and needs no credentials. The same server
also has a browser-backed path for real account reads, publish, delete, and
restore verification.

## Quick start

Requirements:

- Windows PowerShell 7 or PowerShell 5.1
- Python 3.10 or newer, or the Codex bundled Python used by `run_mock.ps1`
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
| MCP tools, resources, prompts | Verified local code | 357-test suite (`quality_gate.ps1`); conformance profile |
| Streamable HTTP sessions | Verified local code | Session, delete, expiry, Origin, and version tests |
| Browser action bridge | Implemented and unit-tested | `browser_bridge.py` |
| Publish and delete protocol | Implemented; real X test is recorded separately | See `../docs/verification/x-starter-mcp-2026-10-21.md` |
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

Each claimed browser action also exposes a context contract for `mobile`,
`tablet`, and `desktop` layouts. Completion can report safe executor metadata,
the locator strategy, and checked preconditions and postconditions. Sensitive
browser fields are rejected. Set `X_STARTER_BROWSER_FAMILY=firefox` for a
controlled Firefox-only executor experiment.

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
$env:X_STARTER_STATE_DIR = "D:\workspace\agent-help\x-starter\.x-starter-state\browser"
python .\server.py --transport http --port 8765
```

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

Result on 2026-10-21: `150 passed, 0 failed, 0 advisory` across handshake,
schemas, errors, robustness, and security. This run uses mock mode and does not
write to X.

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
|-- skill.md                  # Agent-readable onboarding contract
|-- lessons/                  # Six short lessons
|-- templates/                # Post starters and review checklist
|-- examples/                 # Host and transport examples
|-- tests/test_mock.py        # Mock, transport, bridge, and safety tests
`-- ahan-handoff.md           # Credential-free handoff design
```

## Limits

- Browser extraction depends on the signed-in X layout and locale.
- The MCP server does not own a browser. A trusted agent executes queued actions.
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
