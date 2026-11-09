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

<p align="center"><img src="https://raw.githubusercontent.com/MaxEllis/orcaslicer-mcp/main/docs/images/story.png" alt="Three steps: a chat in your AI app above, OrcaSlicer below. 1: you write that you designed these brackets, want them as strong as possible, and ask it to show you the changes it would make; OrcaSlicer shows the bracket standing. 2: the assistant makes three changes and says why: on its side, because standing up the layers run across the arm that takes the load; 7 walls instead of 4, which makes the arms solid; 215 °C instead of 200 so the layers fuse better. OrcaSlicer's MCP panel lists each change with Rotate back or Revert. 3: OrcaSlicer's slices compared: 2h 57m and 33.7 g as designed, 2h 44m and 46.8 g stronger; lying flat saves 22 minutes, more than the extra walls cost."></p>

<p align="center"><sub>Shown with the OrcaSlicer MCP build. The plugin does the settings part.</sub></p>

OrcaSlicer MCP connects an AI app on your computer to OrcaSlicer. You ask in plain words in the AI app, the changes happen in the OrcaSlicer window you already use, and a panel beside the 3D view lists each one with a way to undo it.

## What you need

- **An AI app on your computer that can use MCP connectors,** such as Claude Desktop, Cursor, VS Code or LM Studio. MCP is how these apps connect to other programs, and `orcaslicer-mcp` is the connector for OrcaSlicer. Chats in a web browser and phone apps can't use it, because the connector runs on your computer.
- **OrcaSlicer 2.5** on Windows, macOS or Linux, with one of the two ways in below.
- **About ten minutes.** OrcaSlicer MCP is free. Your AI app works as you already use it, and with a local model nothing leaves your computer.

## Two ways into OrcaSlicer

| | **OrcaSlicer MCP build** | **Plugin** for OrcaSlicer 2.5 |
|---|---|---|
| **What it is** | OrcaSlicer 2.5 with the way in built in | An add-on for the OrcaSlicer you already have |
| **The assistant can** | Change settings and presets, load and arrange models, slice, render the plate, manage plates and projects | Read your setup, change settings, switch presets |
| **The panel** | Every change with an undo, plus restore points | Setting and preset changes with an undo |
| **Get it** | [Download](https://github.com/MaxEllis/OrcaSlicer/releases/latest) | [OrcaCloud](https://cloud.orcaslicer.com/p/EXAMPLE), or the `.py` file on this repo's [latest release](https://github.com/MaxEllis/orcaslicer-mcp/releases/latest) |

The build's installer and Mac app take the place of OrcaSlicer and keep your printers and presets, because both use the same profile folder. To keep stock OrcaSlicer as well, use the portable Windows zip or the Linux AppImage. The plugin leaves your OrcaSlicer as it is.

## Get started

**1. Turn on the way in.**
- **Build:** open **Preferences → Remote API**, tick **Enable Remote API** and copy the token, a password that only your AI app gets.
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

## What you can ask

Goals, in your own words, the way you'd ask a friend who knows OrcaSlicer:

- *"This lid has to press onto a 40 mm jar. What do I change so it fits first time?"*
- *"I designed these brackets and want to understand how to get them as strong as possible. Show me the changes you'd make."*
- *"It's going on a car dashboard. Will PLA survive, and what should I change if I use PETG?"*
- *"The top of this box looks rough. How do I get it smooth?"*
- *"Why does this take six hours, and what can I do about it?"*
- *"Is my print still heating?"* It reads a Klipper or OctoPrint printer, without ever controlling it.

With the plugin, the assistant answers these by changing settings and presets; you load and slice. Every tool is listed in [docs/tools.md](docs/tools.md).

## You stay in charge

- **Every change shows in the panel.** Undo any one, or put the project back to an earlier point (build only).
- **It hears what you change.** Edit something yourself and the assistant is told with its next reply, so it doesn't undo your work.
- **It stays on your computer.** OrcaSlicer listens only locally and wants a token. The assistant can't touch scripts, custom G-code or printer connections unless you allow it in Preferences. [Security details](docs/security.md)

## Privacy policy

orcaslicer-mcp has no backend and collects nothing. It talks to OrcaSlicer and, for the printer tools, your printer, and to nothing else. Your conversation, tool replies included, goes to whichever model your assistant app uses; with a local model nothing leaves your network. [Full privacy policy](docs/privacy.md)

## More

[Tools](docs/tools.md) · [Security](docs/security.md) · [Printers and print history](docs/printer-feedback.md) · [Troubleshooting](docs/troubleshooting.md) · [Development](docs/development.md) · [API reference](https://github.com/MaxEllis/OrcaSlicer/blob/remote-api-250/docs/remote-api/README.md) · [Report a problem](https://github.com/MaxEllis/orcaslicer-mcp/issues)

Free and open source under AGPL-3.0, like OrcaSlicer. If it saves you time, you can [buy me a coffee](https://buymeacoffee.com/maxellis).

<!-- mcp-name: io.github.MaxEllis/orcaslicer-mcp -->
