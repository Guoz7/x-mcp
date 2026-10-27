# X MCP tester links and copy-paste tasks

Use these links when asking another person or Agent to test the project.

## Direct links

- Repository: https://github.com/Guoz7/x-mcp
- Download source ZIP: https://github.com/Guoz7/x-mcp/archive/refs/heads/main.zip
- Prompts: https://raw.githubusercontent.com/Guoz7/x-mcp/main/PROMPTS.md
- Windows runbook: https://raw.githubusercontent.com/Guoz7/x-mcp/main/TESTING.md
- Promotion article: https://raw.githubusercontent.com/Guoz7/x-mcp/main/docs/promotion/x-mcp-200-follow-article.md
- Animation video: https://github.com/Guoz7/x-mcp/blob/main/docs/promotion/x-mcp-200-follow.mp4

The source ZIP is the simplest path. It contains the MCP server and tester
scripts. It does not contain an X login, Cookie, X API key, model key, or
browser profile.

## Windows setup

```powershell
Expand-Archive .\x-mcp-main.zip -DestinationPath .\x-mcp-test
cd .\x-mcp-test\x-mcp-main
tester.cmd setup
tester.cmd doctor
tester.cmd smoke
```

Then run `tester.cmd login` only if the tester wants to test signed-in browser
reads. The human must log in to the dedicated browser profile. The Agent must
never receive the password, Cookie, one-time code, or profile directory.

## Task 1: public read, no login

```text
Use only the xstarter MCP server. First call tools/list.
Call x_public_profile for screen_name="NASA".
Call x_public_timeline for screen_name="NASA" with limit=3.
If a canonical post URL is returned, call x_public_post for that one post.
This is read-only. Do not open a signed-in browser and do not perform any write.
Report tool names, elapsed time, result counts, source URLs, credential disclosure,
and every error code. Do not expose secrets or private data.
```

## Task 2: browser read, no write

```text
Use only xstarter MCP. First call tools/list.
Open https://x.com/home, observe the visible page, capture screenshot metadata,
scroll down 500 pixels, observe again, and close the browser.
Do not publish, like, follow, repost, reply, delete, or enter credentials.
Do not read Cookies, local storage, passwords, tokens, or private browser files.
Report the real title, URL, viewport, login-wall or signed-in state, scroll result,
and every error. If image input is unavailable, say so and do not guess.
```

## Task 3: one reversible like

```text
Use only xstarter MCP and my signed-in account.
Read the owner timeline first and select one canonical post.
Ask for confirmation immediately before liking that one post.
After confirmation, like exactly one post, verify it, then use only a confirmed
safe path to unlike the same post and verify after a fresh navigation.
Never retry an uncertain action. If unlike is not safely supported, stop and report
the missing capability instead of using a generic click.
Return the target URL, action ids, timings, verification method, and final state.
```

## Task 4: one reply that is deleted afterward

```text
Use only xstarter MCP and my signed-in account.
Select one post authored by my own account. Draft a reply containing the unique
marker "xstarter-test-<current-date>" and show me the exact text.
Ask for confirmation immediately before sending it.
After confirmation, send one reply, verify its canonical URL and text hash, delete
that reply, reload the URL, and verify absence. Do not reply to a third party.
Do not send a second reply if any step is uncertain.
```

## Task 5: large batch contract without real writes

```text
Use only xstarter MCP. Call tools/list.
Build a contract-only plan with 200 likes, 10 reposts, 200 replies, and chunk_size=20.
Use synthetic canonical status objects or mock mode. Do not open X and do not call
the start or execute tools. Report operation_count=410, chunk_count=21, counts,
and the first and last sequence numbers. This task must not change any account.
```

## Task 6: small real interaction batch

```text
Use only xstarter MCP and my signed-in account.
Read the owner timeline, select no more than 3 posts, and prepare no more than
3 likes plus 1 reply. Show the exact confirmation phrase and wait for confirmation.
Run one chunk at a time. Stop on uncertain. If the request times out, call only
x_interaction_batch_status and do not execute again.
Report planned, chunk, completed, uncertain, pending, changed, elapsed, and final proof.
```

## Task 7: mutual-follow candidate and batch test

```text
Use only xstarter MCP and my signed-in account.
Call x_follow_candidates_read for my owner handle with limit=200.
Keep only targets whose visible evidence contains both Follows you and Follow back.
Call x_follow_batch_plan with those targets, budget_seconds=600, chunk_size=20,
and require_mutual=true. Report visible_count, mutual_count, operation_count,
chunk_count, and snapshot_sha256. This phase is read-only.
Show the exact phrase from x_follow_batch_prepare and wait for explicit confirmation.
After confirmation, call x_follow_batch_start once or use the compact execute path.
Run one chunk at a time with x_follow_batch_execute. Stop on uncertain, pending,
rate_limited, or failed reload. Never retry an uncertain follow.
Call x_follow_batch_status after each chunk. Count a follow only after reloading
the exact profile and proving Following. Report completed, uncertain, pending,
skipped, changed, elapsed, and the evidence hash.
```

## Bug report

```text
Report client and version, model route, OS, Python, browser, MCP revision,
task number, command, tool, error code, batch id, chunk, counts, elapsed time,
and whether the account changed. Redact API keys, Cookies, passwords, tokens,
browser profiles, private messages, and screenshots with secrets.
```

## Current capability boundary

- Public profile, timeline and post reads: available without an X API key.
- Browser reads: available after the tester logs in to a dedicated profile.
- Likes, reposts and replies: available only through confirmed action leases.
- Large interaction planning: supports 200 likes, 10 reposts and 200 replies in chunks.
- Mutual-follow candidate read: available in browser mode.
- Follow planning, confirmation, leases, reload verification and status: available.
- A 200-follow result: not verified; the current owner snapshot has fewer than 200 mutual candidates.
