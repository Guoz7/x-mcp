# One-click internal test

This package installs a local MCP test environment and generates a client configuration.
It does not contain a model key, X API key, browser profile, cookies, or credentials.

## Fast path

1. Extract the ZIP into a writable directory.
2. Double-click `internal-test.cmd`.
3. Run `tester.cmd login`.
4. Sign in to your own X account in the dedicated browser window.
5. Close that dedicated browser window.
6. Add the generated `mcp-client.json` or `mcp-codex.toml` entry to your agent.
7. Restart or reconnect the agent.

The first run needs Python 3.11 or newer, Google Chrome, and network access to install
the package-local dependencies. The setup does not change global Python, global proxy,
model routing, startup tasks, or another MCP client's configuration.

## Credentials and model routing

The MCP browser path does not need an X API key. The X account is authorized by the
human in the dedicated browser profile. A model provider key belongs to the agent host,
not this ZIP. Set it in the agent's private environment or provider configuration at
runtime. Do not paste it into this package, `mcp-client.json`, a prompt, a screenshot,
or a bug report.

For DSH, configure the model route in DSH itself. The MCP package only supplies the
MCP server and browser action contract. It does not bundle DSH, Magpie, or a provider
runtime.

## First safe prompt

Use the first prompt in `PROMPTS.md`. It is read-only. Do not start with a post, like,
follow, repost, or reply.

## Internal write test

Use the bounded 20-like, 1-repost, 10-reply prompt only after the owner has confirmed
the exact batch phrase. The current verified batch took 133 seconds. It did not prove a
one-minute SLA, 200 follows, 500 replies, or true parallel page execution.

If a long MCP request times out, do not call execute again. Call
`x_interaction_batch_status` with the batch id and use the durable result.
