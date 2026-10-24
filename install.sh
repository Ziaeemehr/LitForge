#!/bin/sh
# One-command install (and update) of LitForge on Linux and macOS:
#   curl -LsSf https://raw.githubusercontent.com/Ziaeemehr/LitForge/main/install.sh | sh
# Gets or updates the source, installs uv if needed, the dependencies, and a LitForge icon.
set -eu

repo="${LITFORGE_REPO:-https://github.com/Ziaeemehr/LitForge.git}"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (Python package manager)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"
  export PATH
fi

# Read only the saved workspace; malformed settings are ignored.
case "$(uname -s)" in
  Darwin) platform_dir="$HOME/Library/Application Support/LitForge/app"
          config_file="$HOME/Library/Application Support/LitForge/desktop.json" ;;
  *) platform_dir="${XDG_DATA_HOME:-$HOME/.local/share}/litforge/app"
     config_file="${XDG_CONFIG_HOME:-$HOME/.config}/litforge/desktop.json" ;;
esac
saved=""
read_saved_workspace() {
  if command -v python3 >/dev/null 2>&1; then
    python3 "$@"
  else
    uv run --no-project python "$@"
  fi
}
if [ -f "$config_file" ]; then
  saved=$(read_saved_workspace -c '
import json, sys
try:
    value = json.load(open(sys.argv[1])).get("workspace", "")
    print(value if isinstance(value, str) else "")
except (OSError, ValueError, AttributeError):
    pass
' "$config_file")
fi
is_checkout() {
  [ -e "$1/.git" ] && [ -f "$1/pyproject.toml" ] && grep -q '^name = "litforge"' "$1/pyproject.toml"
}
if [ -n "${LITFORGE_DIR:-}" ]; then
  dir="$LITFORGE_DIR"
elif is_checkout "$(pwd)"; then
  dir="$(pwd)"
elif is_checkout "$HOME/LitForge"; then
  dir="$HOME/LitForge"
elif [ -n "$saved" ] && is_checkout "$saved"; then
  dir="$saved"
else
  dir="$platform_dir"
fi
case "$dir" in '~') dir="$HOME";; '~/'*) dir="$HOME/${dir#\~/}";; esac

if [ -e "$dir/.git" ] && command -v git >/dev/null 2>&1; then
  say "Updating $dir"
  git -C "$dir" pull --ff-only || fail "could not update $dir (local changes?); run 'git pull' there yourself"
elif [ -e "$dir" ]; then
  [ -f "$dir/pyproject.toml" ] || fail "$dir exists but is not LitForge; set LITFORGE_DIR to another folder"
  say "Using $dir as it is (not a git checkout, so it is not updated)"
elif command -v git >/dev/null 2>&1; then
  say "Downloading LitForge to $dir"
  mkdir -p "$(dirname "$dir")"
  git clone "$repo" "$dir"
else
  say "Downloading LitForge to $dir (no git: using the ZIP of the main branch)"
  tmp="$(mktemp -d)"
  curl -LsSf https://github.com/Ziaeemehr/LitForge/archive/refs/heads/main.tar.gz | tar -xz -C "$tmp"
  mkdir -p "$(dirname "$dir")"
  mv "$tmp/LitForge-main" "$dir"
  rmdir "$tmp"
fi


cd "$dir"
say "Installing dependencies (the first time takes a few minutes)"
uv sync --extra embed --extra documents --extra desktop --inexact  # --inexact: keep extras installed later
extras=",${LITFORGE_EXTRAS:-},"
tty_path="${LITFORGE_TTY:-/dev/tty}"
interactive=false
if [ "${LITFORGE_NO_PROMPT:-0}" != "1" ] && [ -r "$tty_path" ] && [ -w "$tty_path" ] &&
   ( : <"$tty_path" ) 2>/dev/null; then
  exec 3<"$tty_path" 4>>"$tty_path"  # answers in, questions out (also with curl | sh)
  interactive=true
fi

existing=""
if [ -n "$saved" ] && [ -d "$saved/data" ]; then
  existing="$saved"
elif [ -d "$dir/data" ]; then
  existing="$dir"
fi
default_library="${existing:-$HOME/Documents/LitForge}"
library="${LITFORGE_LIBRARY:-$default_library}"
attempt=0
while :; do
  if [ "$interactive" = true ]; then
    printf 'Library folder [%s]: ' "$default_library" >&4
    IFS= read -r library <&3 || library=""
    library="${library:-$default_library}"
  fi
  case "$library" in '~') library="$HOME";; '~/'*) library="$HOME/${library#\~/}";; esac
  if uv run litforge library set "$library"; then
    break
  fi
  attempt=$((attempt + 1))
  if [ "$interactive" != true ]; then
    fail "cannot use library folder $library"
  fi
  if [ "$attempt" -ge 3 ]; then
    library="$default_library"
    uv run litforge library set "$library" || fail "cannot use default library folder $library"
    break
  fi
done
if [ -n "$existing" ] && [ "$library" != "$existing" ]; then
  say "The old library stays at $existing. No files have been moved or copied."
fi
say "Preparing LitForge's own window (no browser needed)"
if ! uv run litforge desktop-setup; then
  printf 'warning: The desktop window is not ready. Retry with: cd "%s" && uv run litforge desktop-setup\n' \
    "$dir" >&2
fi
say "Adding the LitForge icon"
uv run litforge shortcut --mode desktop


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

say "Open LitForge from the LitForge icon in your apps."
printf 'Program: %s\nLibrary: %s\n' "$dir" "$library"
echo "    Run the same command again to update."
echo "    Optional tools: Settings → Optional tools, or run 'uv run litforge tools' in $dir."
