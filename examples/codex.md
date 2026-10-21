# Codex Setup

Use a project-scoped MCP server entry. Keep the token out of this file.

```toml
[mcp_servers.x_starter]
command = "C:\\Users\\54736\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe"
args = [
  "-I",
  "-X",
  "utf8",
  "D:\\workspace\\agent-help\\x-starter\\server.py",
]

[mcp_servers.x_starter.env]
X_STARTER_MODE = "mock"
```

For a live read, the host owner sets `X_STARTER_MODE=live`,
`X_STARTER_ENABLE_LIVE=1`, and `X_BEARER_TOKEN` in the host environment. Do not
add a real token to this tracked example.

The exact config merge path depends on the installed Codex version. Check the
host's `mcp_servers` setting and restart the host after a config change. This
starter does not modify global Codex configuration.

Acceptance for this adapter:

1. `tools/list` returns 17 tools.
2. `account_summary` reports `mode: "mock"` and `simulated: true`.
3. The four-step walkthrough exits 0.

For a real local browser run, use a separate server mode and an executing agent:

```t
X_STARTER_MODE=browser
X_STARTER_ENABLE_BROWSER=1
X_STARTER_STATE_DIR=D:\workspace\agent-help\x-starter\.x-starter-state\browser
```

The agent claims `x_browser_next_action`, uses Computer Use, and returns the
result through `x_browser_complete_action`. The server never receives a browser
cookie, password, token, or one-time code.

If the host reports a transport error, run:

```powershell
python .\server.py --demo
```

That separates server logic from host configuration.
