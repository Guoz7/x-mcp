# Follow evidence and visual readiness audit

## Actual results

Read-only audit of the original ledger found 104 unique completed, changed,
verified Following results, zero duplicate successful targets, 95 uncertain,
4 unresolved claims and 555 pending follow actions. This is not 200 completed
follows. No old uncertain or claimed action was retried or promoted.

The 460-entry evidence hash chain verifies. All 201 completed/uncertain
outcomes (including two account reads) match their corresponding chained
results/errors. This verifies ledger consistency, not an independent replay
of every historical browser interaction or cryptographic proof of X state.

Timing sources for follows:

| Source | Rows | Meaning |
| --- | ---: | --- |
| result.elapsed_seconds | 104 | Recorded browser action time |
| error.details.attempt_elapsed_seconds | 1 | Recorded attempt time |
| claimed_at to completed_at | 94 | Second-resolution ledger elapsed, not browser time |
| Missing | 559 | 555 pending plus 4 unresolved claims |

72 records requested a visual handoff. The original follow ledger contains
zero screenshot/coordinate execution claims and zero proven visual fallback
successes. A handoff request alone must not be called executed recovery.

## Real DSH + DeepSeek visual-readiness run

The runner opened two previously unresolved target profiles, observed each,
called `browser_screenshot(include_data=true)`, and closed the browser.
The run exited 0 after 69.516 seconds, with no new actions or writes.

Both screenshots were captured: 327674 and 182862 bytes, each 1280 by 720.
However, the DSH MCP client returned this exact diagnostic for both images:

```text
model "agentied/deepseek-v4.1-flash" does not declare image input
```

The client checks the current route's `resolveModelInfo().inputModalities`
before passing image attachments to the model. The local route currently
does not declare image input. This proves a capability-admission block, not
that the upstream model can never process images. Do not enable it merely by
claiming capability without a real image-input check.

The model's Follow button reports are DOM-derived, not pixel inspection.
No screenshot was visually interpreted, no write recovery was demonstrated,
and no historical uncertain outcome changed. A capable, owner-approved
visual route or user review remains necessary to prove pixel-level fallback.

## Evidence and tests

Parent workspace outputs:

- `releases/demos/follow-evidence-audit-20261103-235325/audit.json`
- `releases/demos/follow-evidence-audit-20261103-235325/snapshot-metadata.json`
- `releases/demos/dsh-follow-visual-readonly-20261103-234850/visual-readiness.json`

Audit output omits handles, URLs, session IDs and claim tokens. Target/action
references are hashes, not guaranteed anonymous identifiers. Raw live logs
stay local and must not be published.

Command: `python -X utf8 -m unittest tests.test_follow_run_audit -v`.
Six fixture tests pass with exit 0. This is a local audit test, not a live
follow test. The recorded headerless 429 still prevents real writes.
