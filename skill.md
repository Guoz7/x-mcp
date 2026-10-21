---
name: x-starter
description: Connect a human-owned X account through MCP. Observe an account or timeline, prepare a confirmed publish or delete, execute it through a trusted browser bridge, and verify a hash-chained evidence record.
version: 0.3.0
entrypoint: ./run_mock.ps1
mode: mock-by-default
---

# X Starter Skill

Use this skill when a human asks an agent to work with their X account through
this repository. Read the whole file before calling a tool.

## Discover

1. Find `server.py` and `run_mock.ps1` in the same directory as this file.
2. Read the server's `tools/list` response. Do not assume tool names from memory.
3. Confirm that `X_STARTER_MODE` is `mock` unless the human explicitly approved
   a live read.
4. Tell the human which mode is active before showing account data.

## Capabilities

| Tool | Purpose | Side effect |
|---|---|---|
| `account_summary` | Establish a session and inspect the account | Read-only |
| `draft_post` | Create one post draft for review | Local only |
| `schedule_post` | Validate and queue a mock schedule | Mock only; no real job |
| `recent_metrics` | Read recent aggregate metrics | Read-only |
| `disconnect` | Clear the MCP session | Local state only |
| `ahan_handoff` | Prepare a credential-free AHAN join request | No network call |
| `x_oauth_start` | Create an X OAuth 2.0 PKCE authorization URL | Host browser action |
| `x_oauth_status` | Read connected account references and expiry metadata | Read-only |
| `x_oauth_disconnect` | Remove one host-owned token record | Local state only |
| `x_post_prepare_api` | Prepare an API post and confirmation phrase | No X call |
| `x_post_create_api` | Create one post through X API user context | External write |
| `x_session_status` | Inspect mode, account, queued actions, and evidence head | Read-only |
| `x_account_get` | Read or queue an account observation | Read-only |
| `x_timeline_observe` | Read or queue a timeline observation | Read-only |
| `x_post_prepare` | Create an expiring write record and phrase | No external side effect |
| `x_post_publish` | Queue one publish after exact confirmation | External write |
| `x_post_delete` | Prepare or queue a delete with a second confirmation | External write |
| `x_timeline_restore` | Compare a timeline snapshot hash with the baseline | Read-only |
| `x_browser_next_action` | Claim one typed action for Computer Use | Lease only |
| `x_browser_complete_action` | Attach a verified result, failure, or uncertainty | Evidence write |
| `x_browser_action_get` | Read durable action state | Read-only |
| `x_evidence_get` | Validate the evidence hash chain | Read-only |

The X tools and the `ahan_handoff` tool are separate boundaries. Never copy X
credentials, bearer tokens, cookies, or token handles into the AHAN request.

## Human authorization handoff

Before a live operation:

1. State the exact operation, account, and data scope.
2. Ask the human to open the X authorization page in a browser.
3. Let the host complete OAuth 2.0 Authorization Code with PKCE.
4. Let the host store the credential outside this repository.
5. Receive a short-lived session reference in the MCP adapter.

Never ask the human to paste a bearer token into chat. Never put a token in a
tool argument, log, draft, template, or prompt.

Browser mode does not need a token in the MCP process. The MCP server receives
only typed action results. It must never read browser cookies, local storage,
tokens, passwords, or one-time codes.

Live mode now supports host-owned OAuth 2.0 PKCE. Set
`X_STARTER_X_CLIENT_ID` and `X_STARTER_OAUTH_REDIRECT_URI`, call
`x_oauth_start`, and open the returned URL in the owner's browser. The local
HTTP callback is `/oauth/callback`. Tokens remain outside the repository.

API writes require `x_post_prepare_api`, exact human confirmation, and then
`x_post_create_api`. A missing OAuth user token returns `X_AUTH_REQUIRED`; the
server does not fall back to mock data or app-only bearer access.

## Discover the connector runtime first

This server is a connector gateway, not a single X script. Two documents ship
with it:

- `public_skill.md` - install and capability-negotiation guide for other agents.
- `connector-manifest.json` - the connector catalog, execution modes, and
  per-operation confirmation rules.

Call these three tools before choosing an X path:

| Tool | Purpose |
|---|---|
| `connector_list` | Read the connector catalog and declared capabilities |
| `connector_negotiate` | Declare `api`, `computer_use`, `web_intent`, `mcp_apps` |
| `connector_plan` | Get one plan with `execution_mode`, fallbacks, and confirmation |

Rules:

1. Ask for an intent-level operation, such as `read_timeline`, `reply`, or
   `follow`. Do not assume a tool name maps 1:1 to a connector.
2. Honor `execution_mode`. `draft_only` means nothing was executed.
3. Every write plan sets `requires_confirmation`. Ask the human first.
4. `x-official-hosted` is an optional connector and is not connected in this
   build. Do not claim it works.
5. If no connector serves the intent, return a Web Intent link or a draft.
   Never report execution success without a verified result.

## MCP surface

The server implements protocol `2025-11-25` with:

- tools, resources, resource templates, and prompts;
- completion, logging, progress, sampling, and elicitation;
- Streamable HTTP sessions, SSE streams, and session DELETE;
- resource subscription updates over the session SSE stream;
- localhost Host and Origin checks.

Subscribe with `resources/subscribe`, then open `GET /mcp` with
`Accept: text/event-stream`. A successful `x_account_get` or
`x_timeline_observe` call sends `notifications/resources/updated` for
`xstarter://session/current`.

The formal tools declare a shared output schema for the result envelope. The
`data` field remains tool-specific; read the tool description before relying
on its fields.

## Default sequence

1. Call `account_summary`.
2. Call `draft_post` with a concrete topic and tone.
3. Show the complete draft to the human.
4. Call `schedule_post` only after the human approves the text and time.
5. Call `recent_metrics` with a 1 to 30 day window.
6. Call `ahan_handoff` only if the human wants to explore AHAN.

Do not silently publish. Do not turn a mock schedule into a claimed live post.
Do not describe template text as model-generated.

## Browser execution loop

1. Queue `observe_account`. A trusted agent claims the action with Computer Use.
2. Return the verified account fields. Never return raw page HTML or cookies.
3. Queue and complete `observe_timeline`, then retain `snapshot_sha256`.
4. Prepare the exact post text. Show the text, account, hash, and phrase.
5. Queue the publish only after the human approves the exact phrase.
6. Publish once. Verify the canonical URL and exact text hash.
7. Return `uncertain` if you cannot prove the side effect. Do not retry.
8. Prepare delete with a new phrase. Confirm it separately.
9. Delete and verify the canonical post URL is absent.
10. Queue a fresh timeline observation and verify the baseline hash.

Every successful action needs a claimed action id and claim token. Do not
construct or alter a result to make it pass. The server validates account,
canonical URL, exact text, and deletion evidence.

## Structured results

Every tool result has:

```json
{
  "ok": true,
  "tool": "account_summary",
  "mode": "mock",
  "data": {},
  "meta": {
    "simulated": true,
    "real_external_calls": false,
    "trace": {
      "trace_id": "trc_example",
      "events": []
    }
  }
}
```

Errors use:

```json
{
  "ok": false,
  "error": {
    "code": "NOT_CONNECTED",
    "message": "Call account_summary first.",
    "retryable": true,
    "details": {}
  }
}
```

## Error recovery

| Code | Action |
|---|---|
| `NOT_CONNECTED` | Call `account_summary`, then retry |
| `INVALID_ARGUMENT` | Fix the named argument; do not guess silently |
| `LIVE_CONFIG_MISSING` | Stop and ask the host owner to configure the server |
| `LIVE_NOT_ENABLED` | Keep mock mode until the human approves live access |
| `X_AUTH_FAILED` | Ask the host to renew authorization; do not request a token in chat |
| `X_PERMISSION_DENIED` | Report the missing capability; do not retry with wider scope |
| `X_RATE_LIMITED` | Wait, then retry only when the human still wants the read |
| `X_NETWORK_ERROR` | Retry once, then report the failure |
| `LIVE_SCHEDULING_NOT_ENABLED` | Explain that live writes are intentionally blocked |
| `BROWSER_NOT_ENABLED` | Set the explicit browser gate or remain in mock |
| `ACCOUNT_OBSERVATION_REQUIRED` | Observe the account before preparing a write |
| `ACCOUNT_OBSERVATION_STALE` | Observe the account again |
| `ACCOUNT_MISMATCH` | Stop; do not publish or delete with another account |
| `WRITE_EXPIRED` | Prepare a new write and show its new phrase |
| `WRITE_NOT_FOUND` | Re-read `x_post_prepare` output; do not invent a write id |
| `WRITE_NOT_READY` | Read the write state; do not resend a consumed confirmation |
| `CONFIRMATION_MISMATCH` | Stop and ask the human to approve the exact phrase |
| `TEXT_MISMATCH` | Report failure; do not retry the publish |
| `DELETE_NOT_VERIFIED` | Reload the post and report uncertain if absence is unproven |
| `ACTION_NOT_FOUND` | Refresh the action id from `x_session_status` |
| `ACTION_NOT_CLAIMED` | Claim the action before completing it |
| `CLAIM_TOKEN_MISMATCH` | Stop; do not copy a claim token between actions |
| `ACTION_LEASE_EXPIRED` | Report uncertainty; search the marker before any new write |
| `INVALID_ACTION_RESULT` | Re-read the page; do not relax the required fields |
| `BRIDGE_STATE_INVALID` | Stop and ask the operator to inspect the state file |
| `UNKNOWN_TOOL` | Refresh `tools/list` and choose an available tool |

## AHAN handoff

Call `ahan_handoff` with a creator alias, goal, and capability labels. Treat
`https://ahan.local/skill.md` as a configurable placeholder. `status:
"not_contacted"` means no request was sent.

The handoff may contain:

- creator alias;
- goal;
- declared capabilities;
- an explicit `x_credentials_included: false` flag.

The handoff must not contain:

- bearer tokens or OAuth codes;
- cookies or private keys;
- raw account exports;
- hidden system prompts.

Send the request to a separately authorized AHAN Skill endpoint only after the
human reviews it. Keep X and AHAN tool namespaces separate.

## Verification

For mock mode:

```powershell
pwsh -NoProfile -File .\run_mock.ps1
python -m unittest discover -s tests -v
```

For live configuration, run only the local check:

```powershell
python .\server.py --mode live --check-config
```

Do not start a live probe unless the host owner explicitly approves it.
