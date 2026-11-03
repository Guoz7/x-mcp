# DSH image admission: real route and browser checks

Follow-up: [read-only uncertain-follow diagnosis](VISUAL-RECONCILIATION-2026-11-04.md)
records a real model task failure, its corrected run and a reusable output
validator. This did not authorize or perform additional follows.

## Finding and fix

Capturing a screenshot is not the same as delivering pixels to the model.
The prior DSH model entry omitted `input`, so the MCP image adapter refused
the PNG even though the browser captured it successfully.

Before changing that declaration, a synthetic four-color image was sent to
the existing `agentied/deepseek-v4.1-flash` route. Its randomized reading order
was blue, yellow, green, red. The response matched all four positions, returned
HTTP 200 and took 2.266 seconds. Reported usage: 252 prompt tokens, 57 completion
tokens and 368 total tokens; these upstream fields are not internally additive.
The returned route label was `cbcn/deepseek-v4.1-flash`. This verifies the tested
route can handle this image, not the identity of its underlying weights.

The task-local DSH model entry now explicitly declares:

```yaml
models:
  - id: agentied/deepseek-v4.1-flash
    input: [text, image]
```

This belongs inside the existing provider model entry, not at the patch root.
Do not copy this declaration to an untested route. No global provider, model,
proxy, credentials or recurring task was changed.

## Real DSH browser check

Command, from the parent workspace, using the existing read-only runner:

```powershell
$env:DSH_READ_IMAGE_INPUT='1'
$env:DSH_READ_LABEL='dsh-vision-admitted-readonly'
# DSH_READ_PROMPT scoped this run to owner open/observe/screenshot/close only.
& 'E:\Python312\python.exe' -X utf8 .agent-work/run_daily_readiness_dsh.py
```

Result: exit 0, 34.672 seconds. Exactly four calls: browser open, observe,
screenshot and close. Zero new ledger actions and zero writes.

The PNG was admitted into the DSH attachment store rather than replaced with
an image-unavailable diagnostic. It was 1280 by 720, 305550 bytes, SHA-256
`2603fdef037064d6b6b89ca9b904f1d75428247ca66712af29763c3d76d1257f`.
The model described the red-haired illustrated banner, cat avatar and location
of the Edit profile button. The main reviewer opened the actual PNG and checked
these observations. This proves image delivery and useful pixel inspection,
not successful automatic fallback after a failed write.

Private evidence stays in the parent workspace:

- `releases/demos/deepseek-vision-probe-20261104-000209/result.json`
- `releases/demos/dsh-vision-admitted-readonly-20261104-000302/summary.json`
- The latter directory's JSONL and attachment store (not for publication).

## Daily counts and remaining gate

Read-only accounting at 00:06 Asia/Shanghai preserved the original ledger hash.
The 2026-11-03 counts are 104 follows, 0 likes, 0 reposts, 0 replies. The
2026-11-04 counts are 0 in every category. These counts cover this ledger only.
Yesterday's 104 are not today's progress. The target remains 200 per category,
800 total per day; sustainable daily capacity has not been measured.

The original headerless 429 still lacks a recovery condition. Neither midnight
nor readable account pixels prove recovery. No write was attempted or retried.
No background scheduler was installed. A fresh successful owner action or an
explicit platform recovery boundary must be recorded through an audited
recovery procedure before write calibration can resume; that procedure is
still an implementation/acceptance gap.

## Policy corrections and bounded offline checks

A separate review found that ledger-derived policy omitted global-stop and
uncertain records, accepted a missing rate timestamp as epoch zero, and ramped
on unchanged results. The policy now keeps unresolved global stops, considers
every historical rate gate, uses the longest known window, fails closed on
undated relative waits, reduces waves after uncertainty, and ramps only on
verified changed results. Audited recovery and complete-wave progression remain
unimplemented; these changes do not prove daily execution readiness.

Command (repository root):

```powershell
python -X utf8 -m unittest tests.test_follow_orchestration tests.test_daily_action_budget tests.test_rate_gate_clock -v
```

Exit 0: 30 fixture tests, 0.078 seconds, `OK`. No network or external write.
Log: parent workspace
`releases/demos/policy-vision-focused-20261104-000848/tests.log`.
No full repository regression or new real-action capacity test was run.

The actual follow runner preflight was also invoked against the original
ledger and a one-target input. It exited 2 (`blocked`) before starting DSH,
opening the browser or claiming any action. Its reason was
`x_rate_limited_without_recovery_time`; `executed=false`.

No project `/code-review` entry was found. Main-agent diff review and a
separate read-only policy review were used instead. The six new fixture tests
cover the identified failure cases; these reviews do not replace live recovery
or daily-capacity acceptance.

The second review found two more clock boundaries: negative observation
timestamps and conflicting reset/Retry-After headers. Negative timestamps
now fail closed. Both the policy and claim gate use the longer valid window.
The policy intentionally permits later verified fresh-target successes to
raise a target-local uncertainty reduction; it does not retry that target.

Final integrated focused check:

```powershell
python -X utf8 -m unittest tests.test_follow_orchestration tests.test_daily_action_budget tests.test_rate_gate_clock tests.test_follow_stop_policy tests.test_interaction_stop_policy -v
```

Exit 0, 46 fixture tests, 1.086 seconds, `OK`. Log: parent workspace
`releases/demos/policy-rate-final-20261104-001607/tests.log`.
This supersedes the 30-test intermediate check, not an additional 46 live
actions. The parent project's documentation checker still reports its same
six pre-existing stale-index errors; those unrelated indexes were not changed.
