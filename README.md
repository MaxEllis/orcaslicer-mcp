<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MaxEllis/orcaslicer-mcp/main/docs/brand/logo-horizontal-dark.svg">
    <img alt="OrcaSlicer MCP" src="https://raw.githubusercontent.com/MaxEllis/orcaslicer-mcp/main/docs/brand/logo-horizontal-light.svg" width="320">
  </picture>
</p>

<h3 align="center">Tell your AI assistant what the print needs. It finds the OrcaSlicer settings that matter, says why, and shows what they cost before you print.</h3>

<p align="center">
  <a href="https://pypi.org/project/orcaslicer-mcp/"><img alt="PyPI" src="https://img.shields.io/pypi/v/orcaslicer-mcp?color=009789"></a>
  <a href="https://github.com/MaxEllis/orcaslicer-mcp/blob/main/LICENSE"><img alt="License: AGPL-3.0" src="https://img.shields.io/badge/license-AGPL--3.0-555"></a>
  <a href="https://lobehub.com/mcp/maxellis-orcaslicer-mcp"><img alt="MCP Badge" src="https://lobehub.com/badge/mcp/maxellis-orcaslicer-mcp"></a>
</p>

OrcaSlicer MCP connects an AI app on your computer to OrcaSlicer. You ask in plain words in the AI app, the changes happen in the OrcaSlicer window you already use, and a panel beside the 3D view lists each one with a way to undo it.

**1. Say what the print needs**

<p align="center"><img src="https://raw.githubusercontent.com/MaxEllis/orcaslicer-mcp/main/docs/images/step1.png" alt="Your AI app: you write that you designed these brackets, want them as strong as possible, and ask to see the changes it would make. An arrow leads to OrcaSlicer, where the bracket stands on the plate as designed."></p>

**2. It changes what matters, and says why**

<p align="center"><img src="https://raw.githubusercontent.com/MaxEllis/orcaslicer-mcp/main/docs/images/step2.png" alt="Your AI app: the assistant says it made three changes: on its side, because standing up the layers run across the arm that takes the load; 7 walls instead of 4, which makes the arms solid; 215 °C instead of 200 so the layers fuse. A line runs from each reason to OrcaSlicer's MCP panel: Rotated 90° about X (Rotate back), Wall loops 7, was 4 (Revert), Nozzle temperature 215 °C, was 200 (Revert)."></p>

**3. See the trade-off before you print**

<p align="center"><img src="https://raw.githubusercontent.com/MaxEllis/orcaslicer-mcp/main/docs/images/step3.png" alt="Your AI app: the assistant compares OrcaSlicer's two slices, 2h 57m and 33.7 g as designed against 2h 44m and 46.8 g stronger, and notes that lying flat saves 22 minutes, more than the extra walls cost. An arrow leads to OrcaSlicer's preview of the bracket sliced on its side."></p>

<p align="center"><sub>Shown with the OrcaSlicer MCP build. The plugin covers settings and presets only.</sub></p>

## What you need

- **An AI app on your computer that can use MCP connectors,** such as Claude Desktop, Cursor, VS Code or LM Studio. MCP is how these apps connect to other programs, and `orcaslicer-mcp` is the connector for OrcaSlicer. Chats in a web browser and phone apps can't use it, because the connector runs on your computer.
- **OrcaSlicer 2.5** on Windows, macOS or Linux, with the OrcaSlicer MCP build or the plugin (compared below).
- **About ten minutes.** OrcaSlicer MCP is free. Your AI app uses whatever plan you already have, and with a local model nothing leaves your computer.

## OrcaSlicer MCP build or plugin

| | **OrcaSlicer MCP build** | **OrcaSlicer MCP plugin** |
|---|---|---|
| **What it is** | OrcaSlicer 2.5 with the connection built in | A plugin for stock OrcaSlicer 2.5 |
| Read and change settings | ✓ | ✓ |
| Switch presets | ✓ | ✓ |
| Save, rename and delete presets | ✓ | ✗ |
| Load, move and arrange models | ✓ | ✗ |
| Slice, and compare time and filament | ✓ | ✗ |
| See the plate as a picture | ✓ | ✗ |
| Manage plates and projects | ✓ | ✗ |
| Undo each change in the panel | ✓ | ✓ |
| Restore the project to an earlier point | ✓ | ✗ |
| Read your printer's status | ✓ | ✓ |
| **Your OrcaSlicer** | Replaced, with your printers and presets kept | Stays as it is |
| **Updates** | Offered by the build | Automatic from OrcaCloud |
| **Get it** | [Download](https://github.com/MaxEllis/OrcaSlicer/releases/latest) | [OrcaCloud](https://cloud.orcaslicer.com/p/EXAMPLE) or the [latest release](https://github.com/MaxEllis/orcaslicer-mcp/releases/latest) |

The trade-off: the build can do everything but replaces your OrcaSlicer. The plugin keeps your OrcaSlicer but only works with settings and presets. To run the build next to stock OrcaSlicer, use its portable Windows zip or Linux AppImage. A plugin installed from a downloaded file doesn't update itself; your assistant tells you when a newer one is out.

## Get started

**1. Turn on the connection in OrcaSlicer.**
- **Build:** open **Preferences → Remote API**, tick **Enable Remote API** and copy the token (a password for your AI app).
- **Plugin:** subscribe on OrcaCloud and install it from OrcaSlicer's **File → Plugins**, or there choose **Install local plugin** (the arrow beside **Install plugin**) and pick the downloaded file. Tick **Activate** and allow the two permissions it asks for, then copy the token from its **Config** tab. Use one install or the other: each keeps its own token.

**2. Add the connector to your AI app.**
- **Claude Desktop:** download `orcaslicer-mcp-<version>.mcpb` from the [latest release](https://github.com/MaxEllis/orcaslicer-mcp/releases/latest), open it, and paste the token into its settings. Nothing else to install.
- **Other apps (Cursor, VS Code, LM Studio and the like):** install [uv](https://docs.astral.sh/uv/getting-started/installation/) once, a small tool that runs the connector: `curl -LsSf https://astral.sh/uv/install.sh | sh` on macOS and Linux, or `irm https://astral.sh/uv/install.ps1 | iex` in PowerShell on Windows. Then add this to the app's MCP settings:

  ```json
  {
    "mcpServers": {
      "orcaslicer": {
        "command": "uvx",
        "args": ["orcaslicer-mcp"],
        "env": { "ORCA_API_TOKEN": "<your token>" }
      }
    }
  }
  ```

**3. Check it works.** With OrcaSlicer open, ask *"Which printer and filament is OrcaSlicer set up for?"* The answer should name your printer and filament presets. If it can't connect, see [troubleshooting](docs/troubleshooting.md).

## You stay in charge

- **Every change shows in the panel.** Undo any one, or put the project back to an earlier point (build only).
- **It knows what you changed.** When you edit something yourself, the assistant is told with its next reply, so it doesn't undo your work.
- **It stays on your computer.** OrcaSlicer only accepts connections from this computer, and each one needs the token. The assistant can't touch scripts, custom G-code or printer connections unless you allow it in Preferences. [Security details](docs/security.md)

## Privacy policy

orcaslicer-mcp has no backend and collects nothing. It talks to OrcaSlicer and, for the printer tools, your printer, and to nothing else. Your conversation, tool replies included, goes to whichever model your assistant app uses; with a local model nothing leaves your network. [Full privacy policy](docs/privacy.md)

## More

[Tools](docs/tools.md) · [Security](docs/security.md) · [Printers and print history](docs/printer-feedback.md) · [Troubleshooting](docs/troubleshooting.md) · [Development](docs/development.md) · [API reference](https://github.com/MaxEllis/OrcaSlicer/blob/remote-api-250/docs/remote-api/README.md) · [Report a problem](https://github.com/MaxEllis/orcaslicer-mcp/issues)

Free and open source under AGPL-3.0, like OrcaSlicer. If it saves you time, you can [buy me a coffee](https://buymeacoffee.com/maxellis).

<!-- mcp-name: io.github.MaxEllis/orcaslicer-mcp -->
