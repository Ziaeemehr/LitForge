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

Click it and LitForge opens in its own window; no browser and no terminal. Clicking it again brings that
window forward. To stop LitForge, close the window or press **⏻ Quit** at the bottom of the sidebar: your
open file is saved first, and running tasks are cancelled only after you confirm.

The data lives in the `data/` folder of the install folder. Without the icon: `uv run litforge desktop` in
that folder, or `uv run litforge ui` for a browser tab (closing the tab stops it after about two minutes).

### The window

The installer prepares the window's engine (`uv run litforge desktop-setup`; Settings → Optional tools →
LitForge window shows it):

| System | Engine | Download |
|---|---|---|
| Windows | Microsoft Edge WebView2, part of Windows 11 and updated Windows 10 | none; without it the installer says where to get it and the icon opens a browser tab until then |
| macOS | the system's WebKit | none |
| Linux | the system's WebKitGTK (every Ubuntu desktop has it) | none |
| Linux without WebKitGTK | Qt WebEngine | about 210 MB; on X11 it may also need `libxcb-cursor0`, fetched without sudo on Debian and Ubuntu, otherwise the exact install line is printed |

PDF links open in your PDF app and web links in your browser; downloads ask where to save. If the window
cannot start, a message names the log file, `data/logs/desktop.log`. Preferences such as the theme and the
open project are kept between starts.

Tectonic (~22 MB), the LaTeX engine of the Writing Studio, is installed too. Its first compile downloads
about 50 MB of LaTeX packages and takes a few minutes; later compiles take seconds.

## Optional extras

| For | Install |
|---|---|
| AI screening | [Ollama](https://ollama.com/download), then `ollama pull qwen3:4b` |
| Better math, fast | `uv sync --inexact --extra formulas` (`formulas-gpu` on an NVIDIA GPU), in the install folder |
| Better math, best | Ollama, then `ollama pull qwen2.5vl:7b`; choosing Math: high offers to install both. Needs 8 GB of memory; a GPU is optional but without one it takes about a minute per page |
| Better math on an NVIDIA GPU | Nemotron Parse: `litforge tools install nemotron` (about 2.5 GB); Math: high sets it up too. It reads English pages, the Ollama model Persian ones |

`uv run litforge doctor` checks what is installed. Updating keeps these extras.
