# X Starter performance snapshot

Date: 2026-10-27
Version: `0.9.1`
Scope: local protocol, local Streamable HTTP, Chromium fixture, and public X read-only probes.
Credentials: none.
Real X writes during this benchmark: none.

## The short table

| Test | Result | Scope |
|---|---:|---|
| `tools/list` p50 | 0.003 ms | 5,000 in-process mock calls |
| HTTP mock at 512 clients | 994.06 RPS, 0 errors | Local machine; no X network or model |
| HTTP mock at 2,048 clients | 533.08 RPS, 0 errors | Peak memory 658.68 MB |
| Visual fallback checks | 27/27 | Real Chromium; synthetic local page |
| Viewport/DPR/theme matrix | 42/42 | Six synthetic browser contexts |
| Public X profile cold read | 1.31-1.59 s | No OAuth, cookie or API key |
| Public X timeline under rapid pressure | HTTP 429 | External X rate limit observed |
| Historical DSH/X bounded batch | 31 verified actions in 133 s | Internal owner-controlled run; not an SLA |
| Real browser follow cycle | 1.16 s p50 | Five-target reversible sample; click cycle, not server-confirmed success |
| Historical follow verification | 8 / 18 confirmed | 44.4%; separate small run with independent post-action verification |

## How to interpret it

The local MCP layer is not the slow part. Browser startup, page rendering,
network latency, X rate limits and model decisions dominate a real workflow.
The HTTP figures are capacity signals for the local server. They are not X
interaction rates. The browser figures prove the executor guards and geometry
contract. They do not prove every X layout.

The follow result needs special care. The 1.16-second number measures the
browser click cycle. It does not prove that X accepted every write. In a
separate 18-follow run, only 8 states survived independent verification. Do not
convert the click-cycle number into a guaranteed 200-follow result.

## Cost boundary

- Local mock and browser-fixture tests used zero X API credits.
- The pressure driver did not call a model, so model cost was not measured.
- The published tool catalog is about 51,787 bytes, or about 12,947 rough tokens.
- Real API write cost depends on the current X plan and request count.
- Local CPU, memory, browser time and hosting cost depend on the operator machine.

## Reproduce

```powershell
python -X utf8 benchmark.py --iterations 5000
python -X utf8 loadtest.py --clients 512 --requests 10
python -X utf8 executor_visual_e2e.py
python -X utf8 executor_matrix_e2e.py
python -I -X utf8 server.py --demo
```

The complete evidence, including the external public-read probe and raw local
logs, remains in the task workspace. Do not copy private profiles or account
logs into a public release.
