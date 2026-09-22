# Windows install findings — 2026-09-22 (Max's PC, Windows 10 Pro 19045, Defender on, Search indexer on)

Two independent "file in use" failures, both transient locks on freshly written files. Rename-probing
the affected trees seconds later found nothing locked, so the holders are scanners (Windows Search
indexer `SearchProtocolHost.exe` was active; Defender real-time on), not a lingering process.

## 1. `uvx orcaslicer-mcp` fails on pywin32 (12/12 attempts)

uv 0.11.29, Python 3.12 (uv-managed) and 3.14 (system), hardlink and copy link modes, `uvx` and
`uv tool install`, `UV_CONCURRENT_INSTALLS=1`: every run died installing `pywin32-312` (pulled in by
`mcp>=2.2` on win32):

    error: Failed to install: pywin32-312-cp312-cp312-win_amd64.whl (pywin32==312)
      Caused by: failed to remove directory `...\Lib\site-packages\pywin32-312.data`:
      The process cannot access the file because it is being used by another process. (os error 32)

Copy mode failed differently on the same folder ("Wheel contains an invalid entry (directory) in the
`scripts` directory: ...\pywin32-312.data\scripts\.tmpXXXX"). pywin32 is the one dependency with
`.data/scripts/*.py`, which uv moves then deletes immediately after writing; a scanner holding one of
those scripts for a few ms is enough. uv does not retry. `uv tool uninstall` of the half-built env also
fails for the same reason for a while afterwards.

Plain pip succeeded first time (it retries rmtree/rename on PermissionError):

    python -m venv %USERPROFILE%\.venvs\orcaslicer-mcp
    %USERPROFILE%\.venvs\orcaslicer-mcp\Scripts\python -m pip install orcaslicer-mcp

Claude Code was then pointed at `...\Scripts\orcaslicer-mcp.exe` directly. Handshake OK, 44 tools,
`get_status` round-trips to a live OrcaSlicer.

Implications for the README / .mcpb launcher: Windows users with Defender + indexer defaults may hit
this on every fresh version; consider documenting the pip-venv fallback, and (for the .mcpb) retrying
uvx or falling back to `pip` if uvx exits non-zero with os error 32.

## 2. OrcaSlicer MCP 2.4.2-mcp.9 (and the 2.3.2 dev build) fatal on startup with the AppData datadir

    [fatal] OnInit Got Fatal error: boost::filesystem::remove_all: The process cannot access the file
    because it is being used by another process [system:32]: "C:\Users\Max\AppData\Roaming\OrcaSlicer\printers"

Also seen on `...\system\OrcaFilamentLibrary` and `...\system` ("Exception installing bundle Custom").
Three launches in a row failed; a launch 20 minutes earlier had succeeded, so it is a race with the
scanners on the hundreds of profile JSONs rewritten at startup. Upstream behaviour (remove_all with
no retry), not fork-specific, but the fork's users will report it against us.

Workaround in use: `--datadir G:\orca-dev\live-datadir` (copy of the AppData datadir, same token,
port 13130, LAN bind). Drives outside the indexed profile tree do not show the problem. Launcher:
`G:\orca-dev\launch-live.bat`; desktop shortcut "OrcaSlicer MCP".

Possible fork fix: wrap the startup `remove_all` in a short retry loop (e.g. 5 x 200 ms) on error 32,
which is what pip does and what made the difference in finding 1.

## 3. First launch of 2.4.2-mcp.9 shows upstreams Profile syncing change dialog

## 3. First launch of 2.4.2-mcp.9 shows upstream's "Profile syncing change" dialog

Upstream 2.4.0 notice: user profiles now sync via Orca Cloud instead of Bambu Cloud; log in to
migrate, or ignore if you never used Bambu Cloud sync. Inherited from upstream, one-time, not ours.
Verified after dismissing: `sync_user_preset` and `sync_system_preset` stayed `false` (the 3d-printer
repo requires both off); the only config change was a new `"cloud_providers": "orca;bbl"` key.
Worth a line in the fork release notes so users do not report it against the MCP build.
