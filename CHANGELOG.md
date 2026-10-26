# Changelog

This project is currently in the `0.9.x` pre-release line. Entries describe
implemented repository changes, not a promise that every integration is ready
for public hosted use.

## 0.9.1 - internal validation

- Added per-MCP-session browser profiles and lifecycle cleanup.
- Added claim-gated browser writes with observation-bound evidence.
- Added owner login and read checks that never handle passwords or tokens.
- Added bounded interaction batches for likes, reposts, and replies.
- Added credential-free public X reads and an optional Agent Help board.
- Added source-package smoke tests and a credential-free internal tester ZIP.
- Added a plain-language promotion kit, public performance snapshot, and a
  synthetic-account narrated demo source.

Known gaps are tracked in `RELEASE_READINESS.md`.
