# Lesson 1: Connect

**Goal:** Establish a session without putting a credential in the agent prompt.

Run:

```powershell
pwsh -NoProfile -File ..\run_mock.ps1
```

The first result calls `account_summary`. The trace contains an
`auth_boundary` event with `credential_required: false` in mock mode.

Check:

- Does the result say `mode: "mock"`?
- Does `meta.simulated` equal `true`?
- Does the account name include `(mock)`?

In live mode, the human authorizes X in a host-owned browser flow. The adapter
receives a session reference, not a raw token in chat. This starter does not
implement that OAuth flow; it only demonstrates the server-side boundary.
