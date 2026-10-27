# Changelog

This project is currently in the `0.10.x` pre-release line. Entries describe
implemented repository changes, not a promise that every integration is ready
for public hosted use.

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
