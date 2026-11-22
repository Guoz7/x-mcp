# AHAN Handoff

The AHAN hook is a prepared request, not a connection.

## Boundary

| Item | X side | AHAN side |
|---|---|---|
| Tool | `account_summary`, `recent_metrics`, etc. | `ahan_handoff` |
| Credential | Host-owned; never an agent argument | Never included |
| Network | X API only in approved live reads | No call from this starter |
| Result | Account and metric data | Reviewed join request |

`https://ahan.local/skill.md` is a configurable placeholder. This repository
does not claim that the endpoint exists, responds, or accepts a request.

## Request shape

```json
{
  "creator_alias": "mock_creator",
  "goal": "Find one collaboration task after the first post is reviewed.",
  "capabilities": [
    "x_drafting",
    "x_scheduling_review",
    "metrics_reading"
  ],
  "x_credentials_included": false
}
```

## Human review

Before any external join request:

1. Show the exact request to the human.
2. Confirm that the alias and goal are accurate.
3. Remove capabilities the account cannot perform.
4. Confirm that no X credential or token handle is present.
5. Use a separately authorized AHAN connector or browser flow.

## Configuration

Set `AHAN_SKILL_URL` only if a reviewed endpoint exists:

```powershell
$env:AHAN_SKILL_URL = "https://example.invalid/ahan/skill.md"
```

Setting the value does not contact the endpoint. The starter remains offline
for AHAN.
