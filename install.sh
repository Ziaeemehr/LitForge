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
uv sync --extra embed --extra documents --extra desktop --inexact  # --inexact: keep extras installed later
say "Preparing LitForge's own window (no browser needed)"
if ! uv run litforge desktop-setup; then
  printf 'warning: The desktop window is not ready. Retry with: cd "%s" && uv run litforge desktop-setup\n' \
    "$dir" >&2
fi
say "Adding the LitForge icon"
uv run litforge shortcut --mode desktop

extras=",${LITFORGE_EXTRAS:-},"
tty_path="${LITFORGE_TTY:-/dev/tty}"
interactive=false
if [ "${LITFORGE_NO_PROMPT:-0}" != "1" ] && [ -r "$tty_path" ] && [ -w "$tty_path" ] &&
   ( : <"$tty_path" ) 2>/dev/null; then
  exec 3<"$tty_path" 4>>"$tty_path"  # answers in, questions out (also with curl | sh)
  interactive=true
fi

install_optional() {
  tool="$1"
  if uv run litforge tools install "$tool" --yes; then
    say "$tool installed"
  else
    printf 'warning: optional %s installation failed; retry with: cd "%s" && uv run litforge tools install %s\n' \
      "$tool" "$dir" "$tool" >&2
  fi
}

ask_optional() {
  tool="$1"
  prompt="$2"
  case "$extras" in
    *,"$tool",*) install_optional "$tool"; return ;;
  esac
  if [ "$interactive" != true ]; then
    return
  fi
  printf '%s ' "$prompt" >&4
  IFS= read -r answer <&3 || answer=""
  case "$answer" in
    y|Y|yes|YES|Yes) install_optional "$tool" ;;
  esac
}

say "Installing Tectonic, the LaTeX engine of the Writing Studio (~22 MB)"
install_optional tectonic

ask_optional mineru "Install MinerU for accurate PDF reading (~4 GB)? [y/N]"
ask_optional pix2text "Install Pix2Text for better math in exports (~2 GB)? [y/N]"

say "LitForge is installed in $dir. Open it from the LitForge icon in your apps."
echo "    Run the same command again to update."
echo "    Optional tools: Settings → Optional tools, or run 'uv run litforge tools' in $dir."
