# Lesson 5: Measure

**Goal:** Separate evidence from illustrative numbers.

Call `recent_metrics` with `days` from 1 to 30.

Mock mode uses a fixed seven-day baseline and scales it for other windows. Every
response says `SIMULATED`. The numbers are not account data and must not be used
in a performance claim.

Live mode reads recent user posts through X API v2 and aggregates public metric
fields. The implementation is present but was not tested with a credential.
Field availability depends on the token's access.

When reporting a metric, include:

- mode;
- window in days;
- number of posts sampled;
- whether the source is mock or X API v2.
