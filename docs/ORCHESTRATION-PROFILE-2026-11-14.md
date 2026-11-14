# Orchestration profile for one X account

Measured on 2026-11-13/14. Every value here comes from an observed response
during the run; none is a guess. Copy the block and adjust the two measured
limits if a different account behaves differently.

## Why a profile rather than fixed numbers

The three write kinds do not share one limit. Reposts sustain four per wave
indefinitely. Follows have a 900-second window of 15 and, separately, an account
allowance that returns code 161. Likes were refused outright. Treating them as
one budget either wastes the working endpoint or hammers the blocked one.

## Profile

```yaml
account: guoz7qoq
profile_dir: D:\workspace\agent-help\x-starter\.browser-executor-profile

runtime:
  nav_timeout_ms: 45000        # status pages exceed the built-in 15000 on a slow path
  headless: true
  persistent: true

# Follow: the objective's main line.
follow:
  per_window: 14               # 15 is the measured cap; keep one slot spare
  window_seconds: 900
  gap_seconds: 4               # 14 actions take about 60 s
  on_429: wait_reset_epoch     # honours retry-after / reset from the response
  on_code_161: stop            # account allowance, not a timer
  probe_interval_minutes: 150  # one cheap attempt; never a wave

# Repost: the endpoint that currently works.
repost:
  per_wave: 4
  gap_seconds: 20
  on_barren_wave: exponential_backoff_to_300s
  max_barren_waves: 5
  pool_sources:
    - explore_tabs
    - topic_search
    - home_timeline

# Like: refused with code 283 on every attempt.
like:
  enabled: false               # re-enable only after a probe succeeds
  probe_interval_minutes: 240

pool:
  replenish_below: 60          # harvest before the driver reports no fresh posts
  per_harvest: 200
```

## Success contract

A row counts only when all four hold:

```
status == completed
result.changed == true
result.verified == true
result.state in {following, liked, reposted, replied}
```

Nothing else counts. An HTTP 200 without `data` is not success; a DOM change
without a server-backed marker is not success; `pending` and `claimed` are not
success.

## Failure classification

| Observed | Record as | Then |
|---|---|---|
| 2xx/4xx with `errors[]` | `failed` + `X_WRITE_REJECTED` | continue; do not retry |
| 429 | `uncertain` + `X_RATE_LIMITED` | wait `reset_epoch`, then continue |
| dispatched, no proof | `uncertain` | stop that target, never re-click |
| lease expired | `uncertain` + `ACTION_LEASE_EXPIRED` | retire with the tool |

## Ordering rules

1. One owner browser, one MCP session at a time. Two sessions produce
   `BROWSER_OWNER_EXCLUSIVE`, which is a process problem, not an X problem.
2. Run action kinds in sequence, not concurrently, for the same reason.
3. Harvest before a wave when the pool is low; a wave that finds no targets is
   a wasted cycle.
4. Retire expired leases before reading totals, so "in flight" means in flight.

## Signals that must never be read as state

| Signal | Why it lies |
|---|---|
| `Following` anywhere in page text | the profile statistics line always contains it |
| `aria-label` like count | it is the post's total, not the viewer's state |
| `data-testid` numeric prefix | it is the target's user id, not the owner's |
| HTTP 200 alone | X puts refusals inside 200 bodies |

The reliable state markers are the button text (`Following`/`Follow`) for a
follow and `data-testid="unlike"` / `"unretweet"` for a like or repost.
