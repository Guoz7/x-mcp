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
- Apply global stop and response observation to likes, reposts and replies too.
  A follow-only guard does not protect interaction batches. Resume must verify
  the original session, expiry and confirmation, and only claim pending rows.
- Report daily counts in Asia/Shanghai separately per action type. Missing
  account identity means unattributed evidence, not zero observed actions or
  permission to create a fresh ledger. Targets are goals, not proven capacity.
- Screenshot capture is not pixel delivery. Probe the actual model route
  before declaring image support, verify attachment admission and inspect
  the image output. Readable pixels do not prove write-rate recovery.
- A DSH exit code of zero is not task success. Validate the requested output
  contract and retain truncated tool evidence. Visual classification must
  not invent eligibility rules or require Follows-you when only advertised
  mutual-follow text was requested. Keep diagnosis separate from writes.
- Ledger-derived policy must retain unresolved global stops and every rate
  gate. Never substitute epoch zero for an unknown rate timestamp or ramp
  waves on unchanged results. Midnight does not erase account-level gates.
