# Bounded operator window for follow writes

## Why it exists

A recorded X rate limit must stop new writes, even after a restart or a new
batch. That rule stays. Some runs need the owner to state, once and
explicitly, that the account may perform a bounded number of follow actions
until a given time. The operator window records that statement. It does not
delete or rewrite the historical rate-limit evidence.

## Conditions

`browser_bridge.py` accepts a claim under `operator_recovery` only when all of
these hold:

- `mode` is `bounded_follow_run`.
- The window `account_binding` matches the ledger.
- `expires_epoch` is finite and still in the future.
- `used_actions` is below `max_actions`.
- The action type is `follow_profile`.
- No `X_RATE_LIMITED` or `X_SECURITY_INTERSTITIAL` record completed after
  `authorized_epoch`.
- `revoked_reason` is empty.

Each claim increments `used_actions`. A new rate limit or security
interstitial closes the window before another action is claimed. Likes,
reposts, replies, posts and deletes never use this path.

The older single-target permit is narrower: one named handle, one claim,
20 minutes.

## Tools

```powershell
python -X utf8 tools/authorize_operator_window.py `
  --state-dir <account-ledger-dir> `
  --account-binding <binding> `
  --max-actions 200 `
  --minutes 240
```

`tools/authorize_operator_calibration.py` issues the one-target permit. Both
tools write an evidence row and print a JSON receipt. Both refuse a mismatched
account binding.

## What it does not do

- It does not clear, hide or reclassify the original 429.
- It does not extend itself, retry uncertain handles or run on its own.
- It does not bypass the per-target postcondition check. Only
  `completed + changed=true + verified=true + state=following` counts.
- It does not exempt visual clicks from the account quota. The browser still
  calls the same `friendships/create` endpoint.

## Last-slot defect and fix

The claim path spent a slot before the action ran: `next_action()` raised
`used_actions` to 15, then `verify_claim()` re-checked the same window and
rejected the action for being at the limit. The last action of every window
failed against our own counter, not against X.

The window now records every claimed `action_id` in `used_action_ids`. When
`used_actions` reaches `max_actions`, only an action already in that list may
pass its own re-check. A different action still gets rejected, so the limit
keeps its meaning.

Regression test: `tests/test_rate_gate_clock.py::test_last_slot_claim_survives_its_own_verify_recheck`.

Live check on 2026-11-05 17:53: waves of 15 and 14 targets both completed with
the final action verified, which the earlier code could not do.

## Verification

```powershell
python -X utf8 -m unittest tests.test_rate_gate_clock tests.test_follow_batch tests.test_interaction_stop_policy tests.test_follow_stop_policy tests.test_follow_orchestration tests.test_ledger_lock -v
```

Exit 0, 60 test methods, OK. The window cases cover use counting, expiry,
action limit, account mismatch, revocation by a newer rate limit, a rate
record older than the window, and rejection of non-follow actions.

Two deadline tests previously patched the shared `time` module, which also
froze the ledger lock wait clock. They now replace only `server.time`, so the
lock keeps a real clock. This was a test defect, not a runtime change.
