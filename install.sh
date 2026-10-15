#!/bin/sh
# One-command install (and update) of LitForge on Linux and macOS:
#   curl -LsSf https://raw.githubusercontent.com/Ziaeemehr/LitForge/main/install.sh | sh
# Gets or updates the source, installs uv if needed, the dependencies, and a LitForge icon.
set -eu

repo="${LITFORGE_REPO:-https://github.com/Ziaeemehr/LitForge.git}"

# Where to install: LITFORGE_DIR, else this folder when it is a LitForge checkout, else ~/LitForge.
if [ -n "${LITFORGE_DIR:-}" ]; then
  dir="$LITFORGE_DIR"
elif [ -f pyproject.toml ] && grep -q '^name = "litforge"' pyproject.toml; then
  dir="$(pwd)"
else
  dir="$HOME/LitForge"
fi

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

if [ -d "$dir/.git" ] && command -v git >/dev/null 2>&1; then
  say "Updating $dir"
  git -C "$dir" pull --ff-only || fail "could not update $dir (local changes?); run 'git pull' there yourself"
elif [ -e "$dir" ]; then
  [ -f "$dir/pyproject.toml" ] || fail "$dir exists but is not LitForge; set LITFORGE_DIR to another folder"
  say "Using $dir as it is (not a git checkout, so it is not updated)"
elif command -v git >/dev/null 2>&1; then
  say "Downloading LitForge to $dir"
  git clone "$repo" "$dir"
else
  say "Downloading LitForge to $dir (no git: using the ZIP of the main branch)"
  tmp="$(mktemp -d)"
  curl -LsSf https://github.com/Ziaeemehr/LitForge/archive/refs/heads/main.tar.gz | tar -xz -C "$tmp"
  mv "$tmp/LitForge-main" "$dir"
  rmdir "$tmp"
fi

if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (Python package manager)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"
  export PATH
fi

cd "$dir"
say "Installing dependencies (the first time takes a few minutes)"
uv sync --extra embed --inexact  # --inexact: keep extras installed later, such as formulas
say "Adding the LitForge icon"
uv run litforge shortcut

say "LitForge is installed in $dir. Open it from the LitForge icon in your apps."
echo "    Run the same command again to update."
