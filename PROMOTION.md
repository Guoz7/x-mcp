# X Starter promotion kit

This page is the public-facing guide for explaining X Starter in simple terms.
Use the verified numbers below. Keep the scope beside every number.

## One sentence

X Starter lets an MCP-capable agent read X, prepare work, and ask the owner to
confirm state-changing actions. It keeps browser credentials outside the MCP
messages and records action evidence.

## The headline experiment

Use this question as the hook, not as a completed claim:

> Can an agent help you follow 200 people in ten minutes?

The measured answer has two parts. A small real-browser sample produced a
1.16-second median follow cycle. A separate 18-follow run produced only 8
server-confirmed follows after independent verification, or 44.4%. The safe
headline is therefore: **the click path is fast, but server-side success still
needs verification**.

The public article and animation use this boundary explicitly:

- `docs/promotion/x-mcp-200-follow-article.md`
- `docs/promotion/x-mcp-200-follow.mp4`
- `docs/promotion/x-mcp-200-follow-story.json`

## Explain it without jargon

| What people ask | Plain answer | Current boundary |
|---|---|---|
| What does it do? | It gives an agent a controlled X connection. | It is a pre-release local MCP project. |
| Can it read X? | Yes. It can read public profiles and posts without an X API key. | Public pages and X markup can change. |
| Can it use a signed-in account? | Yes, through a dedicated browser profile. | The owner must sign in and confirm writes. |
| Can it see passwords or cookies? | No. The MCP contract excludes browser storage and credentials. | The host must protect the local profile. |
| Can it fall back to screenshots? | Yes. DOM and accessibility come first; visual coordinates are guarded. | The visual test used synthetic pages, not every X layout. |
| Is it a public hosted service? | No. The current package is a local internal tester. | TLS, tenant auth, hosting and operations are not complete. |

## Numbers that are safe to show

| Number | How to say it | Scope that must stay beside it |
|---:|---|---|
| 48 | "48 MCP tools are discoverable in the local demo." | Local `mock` mode, not a promise that every tool is live. |
| 994 RPS | "The local HTTP mock stayed at 994 RPS with zero errors." | 512 clients, local machine, no X network or model calls. |
| 533 RPS | "At 2048 synthetic clients, the local stress run still returned zero errors." | Throughput fell to 533 RPS and memory reached 659 MB. |
| 27/27 | "Three visual-fallback runs passed every guarded check." | Real Chromium on a synthetic page. |
| 42/42 | "Six viewport, DPR, locale and theme cases passed." | Executor compatibility, not proof of every X layout. |
| 31 / 133 s | "An internal DSH/X acceptance verified 31 actions in 133 seconds." | Historical owner-controlled run; not a one-minute SLA. |
| 1.16 s | "The follow click cycle had a 1.16-second median in a five-target sample." | Real browser sample; click cycle, not server-confirmed success. |
| 44.4% | "An earlier 18-follow run independently confirmed 8 server-side follows." | Historical small sample; not a general success rate. |
| 200 / 10 / 200 | "The batch planner accepts 200 likes, 10 reposts and 200 replies, split into chunks." | Interface and contract validation; not 200 real writes. |
| 0 credentials | "The tester package ships without account sessions or keys." | The tester signs in on its own machine. |

## What not to say

- Do not say "works with every agent". Say "any MCP-capable client can use the protocol; visual interpretation depends on the client model."
- Do not say "200 follows in ten minutes" as a completed result. The speed extrapolation exists, but the server-confirmed success rate is not accepted.
- Do not say "500 replies per day". That workload is not accepted.
- Do not call local mock RPS an X action rate.
- Do not present the synthetic `mock_creator` account as a real X account.
- Do not publish a browser recording that shows a personal account, private messages, cookies, or an unredacted profile.
- Do not put an X API key, model key, cookie, or browser profile in the repository.

## Copy for a short post

> We built X Starter: an MCP layer that lets an agent read X, prepare a task,
> ask the owner to confirm writes, and return evidence. The local demo exposes
> 48 tools. In a local HTTP stress run, 512 clients reached 994 RPS with zero
> errors. The package contains no account session or API key. The limits are
> public: this is a pre-release local tester, not a hosted multi-tenant service.

## Copy for a GitHub description

> A credential-free, confirmation-gated X MCP starter with public reads,
> browser fallback, Streamable HTTP, stdio, evidence records, and a local
> tester package.

## Recommended launch sequence

1. Show the narrated video built from `docs/promotion/x-mcp-promo-story.json`.
2. Let viewers run `python -I -X utf8 server.py --demo`.
3. Show the synthetic account label `mock_creator` and the disclosure that no X request was made.
4. Let developers run `tester.cmd smoke` or the unit suite.
5. Link the real evidence table and state the limits in the same post.
6. Invite testers to open issues with the client name, model, OS, tool, error and redacted evidence.

## Test-account demo

The public demo uses `mock_creator`, a deterministic local test account. It
does not log in to X, publish a post, or contact AHAN.

```powershell
python -I -X utf8 server.py --demo > demo-result.json
```

Expected checks:

- `ok` is `true`.
- `mode` is `mock`.
- `tool_count` is `48`.
- The disclosure says that no X or AHAN request was made.
- The account name is `mock_creator`.

This is the right first public demo because anyone can reproduce it without an
account, a key, a browser profile or a model subscription.

## Real-account follow-up

Use a separate owner-controlled test account only after the public demo works.
Keep the account name out of screenshots unless the owner approves it. Run a
read-only check first. Use a separate confirmation for every write. Export
only redacted results for promotion.

## Source files

- `docs/PERFORMANCE.md` contains the short, public benchmark table.
- `docs/promotion/DEMO.md` contains the tester runbook.
- `docs/promotion/x-mcp-promo-story.json` is the source for the narrated video.
- `RELEASE_READINESS.md` is the release boundary. It is more authoritative than a marketing sentence.
