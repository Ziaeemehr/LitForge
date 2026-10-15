# Install and open LitForge

Works on Linux, macOS and Windows. You need [git](https://git-scm.com/downloads) and
[uv](https://docs.astral.sh/uv/); uv also installs the right Python for you.

## 1. Install uv

| System | Command (in a terminal) |
|---|---|
| Linux, macOS | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Windows | `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` |

Open a new terminal afterwards.

## 2. Get LitForge

```bash
git clone https://github.com/Ziaeemehr/LitForge.git
cd LitForge
uv sync --extra embed
```

No git? Download the ZIP from the GitHub page (**Code → Download ZIP**), unpack it and `cd` into it.

`--extra embed` adds meaning-based ranking (a small local model). Leave it out for a smaller install.

## 3. Open it

```bash
uv run litforge shortcut
```

This adds a **LitForge** icon, once:

| System | Where |
|---|---|
| Linux | the app menu (and the desktop, if you have one); pin it to the dock |
| macOS | `LitForge.command` on the desktop; drag it to the Dock |
| Windows | `LitForge.bat` on the desktop |

Double-click it: a small window starts LitForge and your browser opens it. Close that window to
stop LitForge. Clicking the icon again while it runs just opens the browser tab.

The icon uses the data in the folder you ran `shortcut` from (the `data/` folder there). Without the
icon, run `uv run litforge ui` from that folder.

> On a Linux desktop, right-click the icon once and choose **Allow Launching**.

## Optional extras

| For | Install |
|---|---|
| AI screening | [Ollama](https://ollama.com/download), then `ollama pull qwen3:4b` |
| Better math, fast | `uv sync --extra embed --extra formulas` (`formulas-gpu` on an NVIDIA GPU) |
| Better math, best | Ollama, then `ollama pull qwen2.5vl:7b` (about 8 GB of GPU memory) |

`uv run litforge doctor` checks what is installed.

## Update

```bash
git pull
uv sync --extra embed
```

List the same extras you installed with: `uv sync` removes the ones you leave out.
