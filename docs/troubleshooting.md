# If it doesn't start

- **Windows, "The process cannot access the file because it is being used by another process" while installing `pywin32`:** Windows Search or Defender opened a freshly written file mid-install, and uv does not retry. Install with pip instead, which does:

  ```powershell
  python -m venv "$env:USERPROFILE\.venvs\orcaslicer-mcp"
  & "$env:USERPROFILE\.venvs\orcaslicer-mcp\Scripts\python" -m pip install orcaslicer-mcp
  ```

  Then set `"command"` to `C:\Users\<you>\.venvs\orcaslicer-mcp\Scripts\orcaslicer-mcp.exe` with no `args`. Upgrade later with the same pip command plus `-U`.
- **macOS, a GUI client set up with the config file:** apps started from the Dock don't get your terminal's PATH, so `"command": "uvx"` can fail without a message. Run `which uvx` in Terminal and put the full path it prints (usually `~/.local/bin/uvx`) in `"command"`.
- **A client that probes the newer MCP protocol first:** the server answers every stdio connection with the classic `initialize` handshake, because some clients send `server/discover` and then `initialize` anyway, which the MCP SDK would refuse. Set `ORCA_MCP_PROTOCOL=auto` to let the client's first request choose instead.
- **Updates:** take new OrcaSlicer MCP builds from the [releases page](https://github.com/MaxEllis/OrcaSlicer/releases). The build checks there for you; stock OrcaSlicer's own updates would remove the API.
- **OrcaSlicer on another port or another computer:** `ORCA_API_URL` defaults to `http://127.0.0.1:13130`. Set it if you changed the port, or if OrcaSlicer runs on another computer with **Allow LAN access** turned on there.
- **Clients that rewrite their own config file** (Claude Desktop) drop servers added by hand, so use the `.mcpb` extension there.
