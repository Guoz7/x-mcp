# Follow batch guards: 2026-11-03

## Scope and evidence

DSH with DeepSeek executed real owner-account follow actions through the local
browser-mode MCP. Accounts and raw candidate lists are omitted from this public
record. This is a partial live test, not a completed 200-follow benchmark.

| Run | Planned | Attempted | Verified changed | Uncertain | Pending | Execute seconds | Host seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| guarded-wave | 10 | 10 | 10 | 0 | 0 | 75.594 | 193.7 |
| guarded-next14 | 14 | 6 | 5 | 1 | 8 | 47.250 | 197.3 |

Both host processes exited with code 0. That means the runner finished, not that
every planned target succeeded. Each run stayed below its 480-second host budget.

The second run captured HTTP 429 on its sixth target and returned
`stop_reason=X_RATE_LIMITED`. No numeric Retry-After was available in that result.
No remaining target was claimed after that response. The rate-limited target is
uncertain and must not be retried. No further live writes were started.

After restarting MCP with the durable gate, a DSH + DeepSeek check of a new
prepared batch returned `processed=0`, `verified_changed=0`,
`stop_reason=X_RATE_LIMITED`, and `recovery=manual_recovery_required` in 0.031
execute seconds (45.2 host seconds, exit 0). The target remained pending; no
browser or X write was dispatched. The first check failed because the model
used the planner ID instead of the prepared batch ID (BATCH_NOT_FOUND). The
runner instructions were clarified; that failed attempt is retained separately.

The cumulative ledger now contains 104 unique verified new Following results,
95 uncertain records, 4 unresolved claims, and 555 pending records. Pending
records include repeated plans and are not unique people or successful actions.
Earlier uncertain records are not reclassified by these changes.

## Implemented guards

- Browser/runtime, owner exclusivity, security and explicit rate-limit errors
  stop execution regardless of `failure_policy=continue`.
- Browser acquisition failures now enter the action error handler instead of
  leaving a newly claimed action without a terminal record.
- Failed attempts preserve elapsed time. HTTP 429 preserves sanitized response
  metadata and a numeric Retry-After when supplied.
- An unverified dispatched write returns a read-only visual handoff. It never
  recommends a second click. HTTP 200 alone is not Following evidence.
- The DSH runner stops when execute returns a non-null stop_reason.
- A durable account-ledger gate now blocks new X write claims and grants across
  batches, sessions and restarts. Numeric Retry-After expires from recorded
  completion; absent or invalid recovery times require manual recovery.

## Focused verification

Run from the repository root using the bundled Playwright Python:

```powershell
python -X utf8 -m unittest tests.test_follow_stop_policy tests.test_follow_batch tests.test_browser_executor.BrowserExecutorContractTests.test_follow_rate_limit_stops_without_reload_or_second_click tests.test_browser_executor.BrowserExecutorContractTests.test_dispatched_follow_visual_handoff_forbids_second_click tests.test_browser_executor.BrowserExecutorContractTests.test_follow_response_timeout_never_repeats_click -v
```

Integrated result: exit 0, 22 test methods, OK. These are mock-browser policy
checks, separate from the two live runs. No whole-repository regression ran.

Durable-gate follow-up command:

```powershell
python -X utf8 -m unittest tests.test_rate_gate_clock tests.test_follow_stop_policy tests.test_follow_batch -v
```

Result: exit 0, 25 test methods, OK. Coverage includes cross-session reopening,
exact Retry-After expiry, invalid recovery times, read-only admission, existing
write claims, and server stop before browser acquisition. The first run had two
fixture errors (missing public-action metadata); fixtures were completed without
weakening assertions, and the first red log was preserved outside this repo.

## Not yet implemented or established

- Automatic wave adaptation and a confirmed manual recovery/reset workflow.
- Durable visual recovery after the executor browser closes.
- Reconciliation of old unresolved claims.
- A safe numeric account rate or a reliable recovery time for the observed 429.
- Completion of 200 verified follows within a single 20-minute run.

Do not resume writes just by opening a new MCP session or changing candidates.
Honor a supplied Retry-After; when none is available, retain the stop and wait
for an independently established recovery condition rather than probe in a loop.
