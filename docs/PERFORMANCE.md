# X MCP Performance & Verification Snapshot

Date: `2026-11-23` (updated from prior `2026-10-27` baseline)
Version: `0.10.0`
Scope: local MCP protocol core, Streamable HTTP load, real headless Chromium fixtures, local multi-strategy simulation, public X read-only cold probe, and recorded historical External Agent + X audits.

## Measured Scorecard (`v0.10.0`)

| Test / Benchmark | Measured Result | Scope & Exact Conditions |
|---|---:|---|
| 1-command live showcase (`scripts/showcase.py`) | `tools=65`, `219,419 ops/s`, Chromium `1,619 ms`, `173` valid hash-chain entries, `118 -> 175` simulated followers | Local mock + real headless Chrome fixture + synthetic 7-day strategy ledger |
| `tools/list` (`benchmark.py --iterations 5000`) | `181,849.92 ops/s`, `p50 0.004 ms`, `p99 0.023 ms` | 5,000 in-process mock calls (`network_calls=0`) |
| `x_timeline_observe` (`benchmark.py --iterations 5000`) | `105,707.13 ops/s`, `p50 0.007 ms` | 5,000 in-process mock calls (`network_calls=0`) |
| `account_summary` (`benchmark.py --iterations 5000`) | `87,446.83 ops/s`, `p50 0.010 ms` | 5,000 in-process mock calls (`network_calls=0`) |
| Streamable HTTP load (`loadtest.py --clients 512 --requests 10`) | `516.36 RPS`, `0` errors (`0.0%`), `tools/call p50 24.13 ms` | Local Streamable HTTP mock (`65` tools, `75,030` bytes, no X network) |
| Historical HTTP extreme load (`2026-10-27`, 48-tool catalog) | `994.06 RPS` (`512` clients) / `533.08 RPS` (`2,048` clients, `658.68 MB` peak), `0` errors | Local mock mode before 65-tool expansion |
| Real Chromium visual fallback (`executor_visual_e2e.py`) | `9 / 9` checks passed (`0` failed) | Real headless Chrome on local synthetic page + `OBSERVATION_STALE` / `TARGET_NOT_UNIQUE` guards |
| Real Chromium compatibility matrix (`executor_matrix_e2e.py`) | `42 / 42` checks passed across `6` browser contexts | Desktop/Tablet/Mobile viewports, DPR `1.0`–`3.0`, light/dark, EN/ZH/JA/DE/AR/FR |
| Synthetic 200-follow ledger (`follow_pressure.py`) | `200 / 200` completed in `12.471 s` (`16.04 actions/s`, `402` evidence entries, `valid: true`) | Local confirmation, lease, and hash-chain ledger only (no browser/X) |
| Offline unit & contract suite (`unittest discover`) | `701` tests `OK` | All offline unit & contract assertions passing after `0.10.0` edits |
| Public X profile cold read (`2026-10-27`) | `1.31–1.59 s` | Credential-free public web read (no OAuth/cookie/API key) |
| Public X timeline under rapid burst (`2026-10-27`) | `HTTP 429` | External X rate limit observed cleanly |
| Historical External Agent + X follow campaign (`2026-11-03~05`) | `202` unique verified follows across `20` runs (`0` duplicates, `4.0 s` median write) | Real X owner-authorized runs; `100` `uncertain` rows & `3` real HTTP `429` stopped without silent retry ([report](reports/AGENT-FOLLOW-200-2026-11-05.md)) |
| Historical External Agent bounded interactive batch | `31` verified actions in `133 s` (`20` likes, `1` repost, `10` replies) | Single owner-controlled live acceptance batch; not a continuous SLA |

## How to Interpret It

- The local MCP server and hash-chained ledger add sub-millisecond to tens-of-milliseconds overhead.
- Real X throughput is governed entirely by browser cold starts, X page renders, and **human-safe pacing guardrails** (`32–68s` follow micro-waves, `75–145s` reply gaps, `2h` challenge cooldowns) documented in [`orchestration-profile-safe.json`](orchestration-profile-safe.json) and [`reports/POSTMORTEM-SUSPENSION-2026-11-14.md`](reports/POSTMORTEM-SUSPENSION-2026-11-14.md).
- Visual compatibility checks prove executor geometry and postcondition verification cleanly on local synthetic fixtures; real X DOM shifts remain gated by reload verification (`uncertain` never auto-retried).

## Cost Boundary

- Self-hosted Chromium execution and public reads consume **0 X API credits**.
- The full published tool catalog (`65` unique tools) serializes to `75,030` compact JSON bytes (~`18,758` rough `bytes/4` token estimate).
- External LLM inference, local Chrome compute, and optional X Developer API usage depend on the operator's own setup.

## Reproduce Locally

```powershell
python -X utf8 scripts/showcase.py
python -X utf8 scripts/benchmark.py --iterations 5000
python -X utf8 scripts/loadtest.py --clients 512 --requests 10
python -X utf8 e2e/follow_pressure.py --targets 200 --chunk-size 20
python -X utf8 e2e/executor_visual_e2e.py
python -X utf8 e2e/executor_matrix_e2e.py
python -m unittest discover -s tests -p "test_*.py"
```
