# Changelog

This project is currently in the `0.10.x` pre-release line. Entries describe
implemented repository changes, not a promise that every integration is ready
for public hosted use.

## Unreleased

- Added a read-only visual-diagnosis result validator. A real uncertain-profile
  check found a Follow button without retrying the write. The initial model
  eligibility hallucination is preserved as a failed task, despite exit 0.
  See [evidence](docs/VISUAL-RECONCILIATION-2026-11-04.md).

- Corrected ledger-derived pacing for global stops, undated rate records,
  overlapping/conflicting cooldowns, uncertainty and unchanged results. Forty-six focused
  offline checks pass. Real DSH image admission and pixel inspection now work
  on the tested local route; no daily 200-per-category capacity claim is made.
  See [evidence](docs/DSH-VISUAL-READINESS-2026-11-04.md).

- Added a read-only follow evidence audit with strict unique counts,
  hash-chain comparisons, explicit timing sources and visual-handoff gaps.
  A real DSH screenshot check exposed route-level image admission blocking;
  see [audit evidence](docs/FOLLOW-EVIDENCE-AUDIT-2026-11-03.md).

- Added separate daily verified-action accounting for follows, likes, reposts
  and replies (target 200 each), without installing a recurring job.
- Interaction batches now stop on observed 429/security/global faults, bound
  action counts, and validate scoped confirmations on resume. Likes/reposts
  verify after reload. See [evidence and limits](docs/DAILY-ACTIONS-2026-11-03.md).
- Fixed owner display-name parsing to use the profile header instead of a
  keyboard-shortcut tooltip at the start of page text.

- Recorded X rate limits now block subsequent write claims and grants across
  batches/sessions/restarts in the same account ledger. A known Retry-After
  expires by its recorded completion time; missing recovery times fail closed.
- Added a pure evidence-driven wave policy for cooldown, calibration, reset and
  manual recovery decisions. It does not send requests or retry actions.
- Follow batches now stop on global browser/security/rate-limit errors even
  under the continue policy. Error records retain attempt timing.
  See [focused and partial live evidence](docs/FOLLOW-GUARDS-2026-11-03.md).
- HTTP 429 responses halt the batch and preserve numeric Retry-After values.
  Post-write visual handoffs are read-only and preserve sanitized API evidence;
  they never instruct another follow click after an unknown write outcome.
- Fixed `x_follow_batch_execute`: the success path referenced an undefined
  `browser_family`, so every verified follow was recorded as `uncertain` and the
  default `stop` policy ended the batch after the first target. Added a
  regression test with a fake browser.

## 0.10.0 - mutual-follow batch foundation

- Added visible mutual-follow candidate discovery without credential or storage access.
- Added confirmed follow planning, per-target leases, reload verification, status recovery, and uncertain-stop behavior.
- Added package, manifest, public skill, prompt, and performance documentation for follow batches.
- A live 200-follow result remains unverified.

## 0.9.1 - internal validation

- Interaction batches now accept up to 200 likes, 10 reposts and 200 replies in
  one plan. Each plan is split into numbered chunks so a chunk can be
  confirmed, executed and resumed on its own.
- The same confirmed batch can run through the built-in browser executor or
  through an external agent that claims one action at a time with
  `x_browser_next_action` and reports back with `x_browser_complete_action`.
- `x_interaction_batch_status` reports per-chunk progress so a long job can be
  inspected without holding an MCP request open.

- Added per-MCP-session browser profiles and lifecycle cleanup.
- Added claim-gated browser writes with observation-bound evidence.
- Added owner login and read checks that never handle passwords or tokens.
- Added bounded interaction batches for likes, reposts, and replies.
- Added credential-free public X reads and an optional Agent Help board.
- Added source-package smoke tests and a credential-free internal tester ZIP.
- Added a plain-language promotion kit, public performance snapshot, and a
  synthetic-account narrated demo source.

Known gaps are tracked in `RELEASE_READINESS.md`.
