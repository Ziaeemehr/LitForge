# Install and open LitForge

One command installs everything: uv and Python if missing, LitForge, and a LitForge icon in your apps.
Run the same command again to update.

| System | Paste in a terminal |
|---|---|
| Linux, macOS | `curl -LsSf https://raw.githubusercontent.com/Ziaeemehr/LitForge/main/install.sh \| sh` |
| Windows (PowerShell) | `powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Ziaeemehr/LitForge/main/install.ps1 \| iex"` |

LitForge goes to `~/LitForge` (Windows: `%USERPROFILE%\LitForge`); set `LITFORGE_DIR` to choose another
folder. Already cloned it? Run `sh install.sh` (or `.\install.ps1`) inside the clone instead.

## Open and stop

| System | The icon |
|---|---|
| Linux | **Show Apps** and the desktop; right-click → *Pin to Dash* |
| macOS | **Launchpad** (`~/Applications/LitForge.app`); drag it to the Dock |
| Windows | `LitForge` on the desktop |

Click it and LitForge opens in your browser; no terminal stays open. Clicking it again just opens
the page. To stop LitForge, press **⏻ Quit** at the bottom of the sidebar.

The data lives in the `data/` folder of the install folder. Without the icon:
`uv run litforge ui` in that folder.

## Optional extras

| For | Install |
|---|---|
| AI screening | [Ollama](https://ollama.com/download), then `ollama pull qwen3:4b` |
| Better math, fast | `uv sync --inexact --extra formulas` (`formulas-gpu` on an NVIDIA GPU), in the install folder |
| Better math, best | Ollama, then `ollama pull qwen2.5vl:7b`; choosing Math: high offers to install both. Needs 8 GB of memory; a GPU is optional but without one it takes about a minute per page |

`uv run litforge doctor` checks what is installed. Updating keeps these extras.
