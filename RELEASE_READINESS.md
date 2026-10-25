# Public Release Readiness

Status: `pre-release / not approved for public hosted deployment`

This checklist is the release source of truth. A green local test suite does
not make the service a hosted multi-tenant product.

## What is ready

- Source repository is self-contained and uses portable paths.
- MCP stdio and Streamable HTTP adapters are covered by local tests.
- The server has an offline mock mode that needs no credentials.
- The browser path keeps profiles and state outside the repository.
- Write actions require an exact confirmation phrase, a claim, and an
  observation-bound result.
- The internal ZIP builder uses an allowlist and excludes credentials, browser
  profiles, local state, and the user-owned test HTML file.
- CI checks, release checks, contribution guidance, and security reporting are
  present in this repository.

## Verified evidence

| Check | Current evidence | Meaning |
|---|---|---|
| Python test suite | 480 tests, exit 0 in the current working tree | Offline contracts and local behavior |
| MCP conformance | Official profile 39/39 in the recorded run | Protocol behavior for the tested profile |
| `checkmcp` | 94/94 in the latest release verification | Schema, error, robustness, and security checks for that run |
| DSH/X owner batch | 20 likes, 1 repost, 10 replies; 31 verified actions | One owner-controlled acceptance run, not a scale guarantee |
| One-minute batch target | Not verified | The measured action window was 133 seconds |
| Page-level parallelism | Not implemented | Current executor is serial |

Local acceptance logs and account-specific evidence are intentionally not
committed here. Re-run the commands in `TESTING.md` against the exact commit
being released and attach redacted CI or release artifacts separately.

## Release blockers

The following items must be resolved before calling the repository a public
hosted service:

1. Choose and commit an explicit open-source license. Until then, the source is
   not automatically available for reuse. See `LICENSE-DECISION.md`.
2. Provide a real deployment with TLS, host authentication, tenant isolation,
   rate limits, observability, backup, and an operator runbook.
3. Replace the local JSON token store with an approved OS or service secret
   store for any hosted deployment.
4. Run a fresh release-candidate acceptance for OAuth, owner browser reads,
   confirmed writes, recovery, and rollback.
5. Decide whether high-volume workloads are supported. The current code does
   not claim 200 follows, 500 replies per day, or a one-minute 31-action SLA.
6. Deploy and independently test the public Agent Help board if it is part of
   the product promise. The repository currently ships only the local provider.

## Pre-tag checklist

- [ ] License selected and present as `LICENSE`.
- [ ] Version matches `server.py`, `connector-manifest.json`, and the release notes.
- [ ] `python release_check.py --require-license` passes.
- [ ] `python -m unittest discover -s tests -p "test_*.py"` passes.
- [ ] `git diff --check` passes.
- [ ] Official MCP conformance passes against the release candidate.
- [ ] `checkmcp` passes with zero failed checks and zero advisories.
- [ ] `python build_test_package.py --output <new-output>.zip` passes.
- [ ] Package contents and SHA-256 are reviewed by a second person.
- [ ] No credentials, cookies, browser profiles, local state, or private logs
      are in the commit, tag, release assets, prompts, or screenshots.
- [ ] README claims and evidence dates have been reconciled with this commit.
- [ ] A local commit has been reviewed before any remote push.

## Publication rule

Do not push or create a public release from a dirty working tree. First create
a reviewed local commit, inspect the exact tree, then obtain explicit approval
for the target remote and tag. This project does not infer a GitHub repository
or publish destination from the local checkout.
