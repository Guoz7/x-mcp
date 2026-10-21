# Lesson 4: Schedule

**Goal:** Validate a post and queue a reviewable record.

Call `schedule_post` with the approved content and an ISO 8601 time that
includes a time zone.

Mock mode returns `status: "mock_scheduled"`. No platform scheduler is running.
No post will publish when the time arrives.

Live mode returns `LIVE_SCHEDULING_NOT_ENABLED`. This is intentional. A real
schedule needs:

- a host-owned durable queue;
- user-context write authorization;
- a separate human approval;
- idempotency and failure handling.

Do not say "post scheduled" without naming the mode. Say "mock schedule created"
or "review record created" when using this starter.
