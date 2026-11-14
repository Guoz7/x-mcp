# X write endpoints: measured behaviour on one account

Measured on 2026-11-13 and 2026-11-14 against a single signed-in account
(`guoz7qoq`) through the browser executor. Every number below comes from a real
page or a real API response captured during the run, not from documentation.

## The three write kinds behave differently

| Kind | Endpoint | Result | Evidence |
|---|---|---|---|
| Repost | `CreateRetweet` | works | 736 verified `reposted`, about 4 of 4 per wave |
| Like | `FavoriteTweet` | refused | HTTP 200 + `errors[{code:283}]` "denied by scarecrow", 56 times |
| Follow | `friendships/create.json` | refused | HTTP 403 + `errors[{code:161}]` "unable to follow more people", 8 times |

A refusal is not a rate limit. The like responses carried
`x-rate-limit-limit: 500` and `x-rate-limit-remaining: 471` while refusing every
single write, so the quota was nowhere near exhausted.

## What a refusal looks like

Both shapes carry an `errors` array, and both are definite answers:

```json
// like, HTTP 200
{"data":{},"errors":[{"code":283,"kind":"Permissions",
  "message":"Authorization: User 1 favoriting tweet 2 was denied by scarecrow"}]}
```

```json
// follow, HTTP 403
{"errors":[{"code":161,
  "message":"You are unable to follow more people at this time."}]}
```

Before this was handled, both were recorded as `ACTION_UNCERTAIN`, which reads
as "the write may have landed". It had not: the button stayed on `Follow` and
the post's action bar kept `like` rather than `unlike`.

## The follow quota is separate from the follow allowance

Two different limits apply to follows and they must not be confused:

- **Per-window quota**: `limit: 15`, `remaining`, `reset` on the 900-second
  window. Exceeding it returns HTTP 429 with `retry-after`. Wait for
  `reset_epoch`, then continue.
- **Account allowance**: code 161 with HTTP 403. This one does not clear on a
  timer measured in minutes. It appeared after roughly 215 follows on a new
  account and was still refusing many hours later.

## Reading a follow's result from the page

Three signals exist and only one is reliable:

| Signal | Reliable | Why |
|---|---|---|
| Button text `Following` vs `Follow` | yes | Direct render of the account state |
| `data-testid` suffix `-unfollow` | yes | Suffix names the state; the numeric prefix is the **target's** user id |
| Substring `Following` in page text | no | The profile statistics line always contains "Following", so it matches on every profile |

## Reading a like's result from the page

`data-testid="unlike"` is the marker that appears after a like lands. The
`aria-label` is **not** usable as a state signal: it reads "6 Likes. Like" on a
post the account has liked and "0 Likes. Like" on one it has not, so the number
is the post's total, not the viewer's state.

## Pacing that avoided the 429

A follow wave of 14 actions spread over about 60 seconds stayed inside the
900-second window. A second wave started immediately afterwards hit 429 at the
4th action. Reposts sustained 4 per wave with a 20-30 second pause between
waves and never met a limit.

## Instrumentation that made this measurable

- `X_BROWSER_EXECUTOR_NAV_TIMEOUT_MS` (1000-60000): a cold Chrome start plus a
  status page on a slow path exceeds the built-in 15 s.
- The status-page readiness check waits for a link to the requested status, not
  for "any article": recommendation cards paint first.
- `tools/retire_expired_leases.py` retires claims whose lease expired, which the
  bridge otherwise leaves stuck when a batch is never resumed.

## What this does not establish

These numbers describe one account on one network path on two days. They do not
establish a platform-wide quota, and the account allowance may depend on
account age, follower ratio and history rather than on a fixed count.
