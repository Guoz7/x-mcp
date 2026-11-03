# Follow batch recovery rules

- Preserve completed, uncertain, and unresolved action evidence; do not retry a
  dispatched write just because its visible postcondition is unknown.
- `failure_policy=continue` only permits independent target-local failures.
  Browser runtime, owner exclusivity, security verification and rate limits
  must stop the batch before another target is claimed.
- After a possible write, visual fallback is read-only. Never offer a second
  click in the fallback instruction. HTTP 200 alone is not Following evidence.
- Record action timing on failure as well as success. A screenshot handoff
  record is not evidence that visual recovery actually executed.
- Use isolated temporary state and a mock browser for policy tests. Keep live
  DSH/DeepSeek test evidence separate and enforce the 20-minute host deadline.
- Account rate limits survive new batches, sessions and MCP restarts in the
  same account-bound ledger. Enforce them before claim and grant issuance.
  Unknown recovery times require a confirmed recovery condition, not an
  invented timer, cleared ledger, changed candidate or repeated probe.
- Use evidence-driven wave sizes `1 -> 3 -> 5 -> 10` only after verified
  calibration successes. After cooldown expiry, allow one fresh-target
  calibration action. A missing recovery time means manual recovery, not a
  guessed wait.
