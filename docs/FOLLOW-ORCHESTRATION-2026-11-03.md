# Evidence-driven follow orchestration

## Policy

The scheduler uses the X response as the control signal. It does not add
random likes, browsing, comments, or fake human behavior.

1. A recorded 429 with a valid `Retry-After` or absolute reset header creates a durable cooldown.
2. No write claim is allowed before that deadline.
3. At the deadline, run exactly one fresh-target calibration action.
4. Verified waves grow through `1 -> 3 -> 5 -> 10`.
5. A new 429 resets the wave to `1` and starts a new cooldown.
6. A 429 without a usable recovery header enters `manual_recovery`; no timer is guessed.
7. A local uncertain result is not treated as a rate limit, but its target is
   final and cannot be retried.
8. Security, browser-global, and owner-exclusivity errors stop the write run.

## Current account state

The authoritative ledger has 104 unique verified Following results, 95
uncertain records, 4 unresolved claims, 555 pending records, and one recorded
429 without a recovery time. The current decision is therefore:

```json
{
  "allow_write": false,
  "wave_size": 0,
  "calibration_required": true,
  "reason": "x_rate_limited_without_recovery_time"
}
```

This state must not be cleared by changing the candidate file, starting a new
MCP session, or restarting the service.

## Implementation

`follow_orchestration.py` is a pure policy module. It does not open a browser,
send requests, wait, or retry actions. The live browser ledger remains the
authority for claims and postconditions. The next integration step is to call
this policy before creating a write batch and to require a fresh target for the
calibration action.

The browser executor now records only safe rate-limit metadata from a 429:
`limit`, `remaining`, `reset_epoch`, and `retry_after_seconds`. It never stores
cookies, tokens, or raw response headers. The ledger gate prefers the recorded
`Retry-After`, then the absolute reset time, and otherwise blocks for manual
recovery.

## Verification

```powershell
python -X utf8 -m unittest tests.test_follow_orchestration -v
```

Result: 35 focused tests passed, exit code 0. The tests cover known cooldown expiry,
unknown recovery, ramp-up, reset after a new 429, local uncertainty, no fresh
target, ledger-derived manual recovery, reset-header recovery, durable gate
checks, follow stop policy, batch execution, and safe 429 header capture.

The live orchestration preflight was also run against the current account
ledger. It selected no target, made no DSH call, claimed no action, and wrote
`releases/demos/dsh-follow-orchestration-preflight-check-20261103-191254/orchestration-preflight.json`.
The reason was the existing 429 without a recovery time.

The policy does not claim that the X limit has recovered. It only makes the
next decision explicit and fail-closed.
