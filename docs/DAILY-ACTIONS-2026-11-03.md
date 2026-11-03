# Daily action target and bounded verification

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
