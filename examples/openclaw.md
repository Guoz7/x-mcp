# OpenClaw Setup

OpenClaw has multiple MCP configuration styles across releases. Use its
documented local command-server option and keep the starter's stdio contract:

```text
command: <python executable>
args: -I -X utf8 <PATH_TO_X_STARTER>\server.py
env:
  X_STARTER_MODE=mock
```

If your installed OpenClaw build only supports HTTP MCP, start:

```powershell
python .\server.py --transport http --port 8765
```

Then register `http://127.0.0.1:8765/mcp` as a Streamable HTTP MCP endpoint.
The server issues `Mcp-Session-Id`, validates local Host and Origin headers, and
uses SSE for server-to-client requests or progress.

Do not paste `X_BEARER_TOKEN` into an agent prompt. Put it in the process
environment owned by OpenClaw, only after the human approves live access.

The exact OpenClaw field names were not verified against an installed release
in this task. Use `server.py --demo` and a raw JSON-RPC request to diagnose the
server independently from the host.
