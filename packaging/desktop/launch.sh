#!/bin/sh
# LitForge launcher of the macOS .app and the Linux .deb (scripts/build_desktop.py fills in @VERSION@ and @RES@).
# The first start, an update or a moved .app (re)builds the per-user runtime with the bundled uv, then the window
# opens. Library data is never touched here.
VERSION="@VERSION@"
@RES@
case "$(uname -s)" in
  Darwin) RUNTIME="$HOME/Library/Application Support/LitForge/runtime" ;;
  *) RUNTIME="${XDG_DATA_HOME:-$HOME/.local/share}/litforge/runtime" ;;
esac
STAMP="$VERSION $RES"
LOG="$RUNTIME/setup.log"

tell() {  # $1 = message, $2 = "error" for a dialog the user must close
  if [ "$(uname -s)" = Darwin ]; then
    msg=$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')
    if [ "$2" = error ]; then
      osascript -e "display dialog \"$msg\" with title \"LitForge\" buttons {\"OK\"} with icon stop" >/dev/null 2>&1
    else
      osascript -e "display notification \"$msg\" with title \"LitForge\"" >/dev/null 2>&1
    fi
  elif [ "$2" = error ] && command -v zenity >/dev/null 2>&1; then
    zenity --error --title=LitForge --text="$1" >/dev/null 2>&1
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send LitForge "$1" >/dev/null 2>&1
  fi
  printf 'LitForge: %s\n' "$1" >&2
}

if [ "$(cat "$RUNTIME/stamp" 2>/dev/null)" != "$STAMP" ] || [ ! -f "$RUNTIME/state.json" ]; then
  mkdir -p "$RUNTIME" || { tell "Cannot create $RUNTIME" error; exit 1; }
  if ! mkdir "$RUNTIME/.setup-lock" 2>/dev/null; then
    tell "LitForge is still being set up; it opens when that is done."
    exit 0
  fi
  trap 'rmdir "$RUNTIME/.setup-lock" 2>/dev/null' EXIT INT TERM
  tell "Setting up LitForge $VERSION. The first time this downloads about 100 MB and takes a minute or two."
  if env -u VIRTUAL_ENV "$RES/bin/uv" run --no-project --python 3.12 "$RES/provision.py" \
      --app "$RES/app" --runtime "$RUNTIME" --uv "$RES/bin/uv" --extra desktop >"$LOG" 2>&1; then
    printf '%s' "$STAMP" >"$RUNTIME/stamp"
  else
    tell "LitForge could not finish its setup (an internet connection is needed the first time). Open it again to retry. Details: $LOG" error
    exit 1
  fi
  rmdir "$RUNTIME/.setup-lock" 2>/dev/null
  trap - EXIT INT TERM
fi
exec "$RUNTIME/venv/bin/litforge-app" "$@"
