# Raw Streamable HTTP MCP

Start the local adapter:

```powershell
python .\server.py --transport http --port 8765
```

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

Initialize and keep the returned session:

```powershell
$init = @{
  jsonrpc = "2.0"
  id = 1
  method = "initialize"
  params = @{ protocolVersion = "2025-11-25" }
} | ConvertTo-Json -Depth 5

$response = Invoke-WebRequest `
  -Method Post `
  -Uri http://127.0.0.1:8765/mcp `
  -ContentType "application/json" `
  -Body $init

$session = $response.Headers["Mcp-Session-Id"]
$headers = @{
  "Content-Type" = "application/json"
  "Mcp-Session-Id" = $session
  "MCP-Protocol-Version" = "2025-11-25"
}
```

List tools:

```powershell
$body = @{
  jsonrpc = "2.0"
  id = 2
  method = "tools/list"
  params = @{}
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8765/mcp `
  -Headers $headers `
  -Body $body
```

Call `account_summary`:

```powershell
$body = @{
  jsonrpc = "2.0"
  id = 3
  method = "tools/call"
  params = @{
    name = "account_summary"
    arguments = @{}
  }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8765/mcp `
  -Headers $headers `
  -Body $body
```

The response includes `result.structuredContent`, not a plain tool payload.
Mock results have `meta.simulated: true`.

For a faster check, use `python .\mcp_client.py --tool x_account_get`.

When done, end the session:

```powershell
Invoke-WebRequest `
  -Method Delete `
  -Uri http://127.0.0.1:8765/mcp `
  -Headers @{ "Mcp-Session-Id" = $session }
```
