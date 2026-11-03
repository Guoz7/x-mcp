# Daily action target and bounded verification

2026-11-04 follow-up: [visual readiness and policy corrections](DSH-VISUAL-READINESS-2026-11-04.md).
The tested DSH route now admits images. Today's counts start at zero; the
104 verified follows below belong to 2026-11-03, not the new accounting day.

## Target, not a capacity claim

The requested daily target is 200 new verified actions each for follows,
likes, reposts and replies: 800 in total. This is not a measured sustainable
X allowance. No recurring job was installed in this change.

`daily_action_budget.py` reads a ledger snapshot. It counts each action type
separately in Asia/Shanghai and deduplicates account/type/target. Uncertain,
claimed, failed, unchanged, other-day and unverified results do not count.
Replies additionally require the canonical reply URL and confirmed text hash.
This report does not enforce a daily execution quota or launch any action.

## Fixes in this change

- Like, repost confirmation and reply submit observe their browser write
  response and classify an observed HTTP 429 as a durable account stop.
- Interaction batches stop on rate/security/browser-global faults even with
  the continue policy. Owner acquisition failures retain their evidence.
- Each interaction execute call has an action limit (default 10) and a
  before-claim deadline (default 1155 seconds). These checks do not interrupt
  an already-dispatched action; the host must still enforce the 20-minute cap.
- Resuming a partial interaction batch checks session, original confirmation
  and expiry, then claims pending rows only.
- Likes and reposts reload for verification even after optimistic UI success.
- Owner display names come from the profile header, not the first body line.

## Local tests

Command (repository root):

```powershell
python -X utf8 -m unittest tests.test_interaction_stop_policy tests.test_daily_action_budget tests.test_interaction_batch tests.test_follow_stop_policy tests.test_follow_orchestration tests.test_rate_gate_clock tests.test_browser_read.VisibleProjectionTests -v
```

Result: `Ran 57 tests`, `OK`, exit 0. These are isolated fixtures, not 57
real X actions. Log: parent workspace
`releases/demos/daily-actions-final-20261103-234239/tests.log`.

## Real DSH + DeepSeek read

The same owner profile and original ledger were used. No fresh write ledger
was substituted. After a read-only `x_owner_read(target=account)` followed by
`browser_close`, the owner identity matched and the display-name defect was
fixed and rechecked. The final run exited 0 in 45.672 seconds, with one new
`observe_account` action and zero new writes. Evidence: parent workspace
`releases/demos/dsh-daily-readiness-20261103-234203`.

The live profile was headless. The temporary MCP server was stopped at the
end; no scheduler, monitor or startup entry was registered.

## Current original-ledger accounting

On 2026-11-03 Asia/Shanghai, the ledger contains 104 verified changed follows
and no like/repost/reply records. Remaining targets are 96 / 200 / 200 / 200.
These are this ledger's counts, not a claim about all account activity.

The read refreshed the missing account observation/binding without removing
any historical action. The pre-existing 429 still has no recovery boundary,
so the report remains blocked. Login success and successful reads do not
prove write recovery. No calibration write occurred in this change.

## Remaining acceptance gaps

- Explicit, auditable recovery evidence for the headerless 429.
- Live small-wave interaction verification after that recovery.
- Daily dispatch integration, content relevance checks and measured pacing.
- A multi-day record with each category reaching its target.

A missing/mismatched write endpoint can time out without identifying a 429.
That outcome remains unknown; it is not proof of rate recovery. This change
does not claim all X response variants or daily throughput have been tested.

## 2026-11-04 independent-wave correction

The planner, MCP schema and preparation previously required at least one
like even for a repost-only or reply-only wave. Reposts also recycled the
like list: requesting one like and ten reposts planned the same repost ten
times. Both defects are fixed. Nonempty zero-like waves are supported;
reposts use distinct observed target URLs. Existing per-batch ceilings stay
unchanged. A daily goal of 200 reposts requires separate bounded batches,
not raising the per-batch ceiling.

Focused command, from this repository:

```powershell
python -X utf8 -m unittest tests.test_interaction_independent_waves tests.test_interaction_batch tests.test_interaction_stop_policy tests.test_daily_action_budget tests.test_rate_gate_clock -v
```

Result: 44 tests, OK, exit 0; unittest elapsed 1.154 seconds. Log in parent
workspace: `releases/demos/independent-waves-20261104-0100/tests.log`.
One new fixture plans 200 likes, 200 reposts and 200 replies in independent
ten-action waves. This is 600 planned fixture operations, **zero real writes**.
The MCP execution fixtures verify all three interaction types stop before
browser acquisition under a recorded headerless 429.

The legacy local interaction runner was found using a new state directory
for each demo. It now reads the original ledger before starting DSH/server
or reading model credentials, and refuses batches exceeding remaining daily
goals or the policy wave. It is not a daily dispatcher. The original follow
runner and corrected interaction runner both exit 2 at the real preflight.
The authoritative state SHA-256 remained
`c31fbeaad22e9346f6332ae5f8560d471bf5ad5e2fb5b6d00c2bdd76d59268a7`.

At 2026-11-04 00:56 Asia/Shanghai the original ledger reports 0 verified
actions in each of four categories today. Yesterday's 104 follows are not
today's count. The same headerless 429 remains unresolved: no automated
calibration, like, repost, reply or follow was dispatched in this change.
An independently confirmed recovery condition is still required. Reading
pages, admitting screenshots or reaching midnight cannot supply it.

Daily execution dispatch, runtime quota reservation and automatic visual
reconciliation remain integration gaps. No schedule was created and no
200-per-category sustainable daily throughput has been measured.
