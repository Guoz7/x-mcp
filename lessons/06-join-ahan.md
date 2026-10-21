# Lesson 6: Join AHAN

**Goal:** Prepare a community join request without moving X credentials.

Call `ahan_handoff` with:

- a creator alias;
- one clear goal;
- declared agent capabilities.

The result says `status: "not_contacted"`. It includes a request object with
`x_credentials_included: false`.

`https://ahan.local/skill.md` is a placeholder. Do not claim that a live
endpoint exists. Do not send the request until the human reviews its alias,
goal, capabilities, and privacy content.

The X and AHAN tool namespaces stay separate:

```text
X tools -> account and content operations
ahan_handoff -> credential-free community request
```
