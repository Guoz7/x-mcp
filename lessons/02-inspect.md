# Lesson 2: Inspect

**Goal:** Read the account context before writing.

The `account_summary` result contains:

| Field | Use |
|---|---|
| `user_id` | Stable account identifier |
| `username` | Human-facing handle |
| `display_name` | Name shown in the review |
| `bio` | Context for tone and topics |
| `metrics` | Follower, following, and post counts |
| `disclosure` | Simulated or live evidence label |

Do not infer audience quality from follower count. It is a count, not an
engagement measure.

The mock profile uses illustrative integers so the flow has stable output. It
is not a real person and does not represent market research.
