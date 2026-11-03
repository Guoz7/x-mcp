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
