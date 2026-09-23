#!/bin/bash
cd "$(dirname "$0")"

# Finder-launched .command files have a tiny PATH. Homebrew / python.org installs
# would otherwise look "missing" even when Python is installed.
export PATH="/opt/homebrew/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/Current/bin:$PATH"

PORT=8768
SESSION_FILE="/tmp/dcloud-content-manager-${PORT}.session"

bundled_python() {
  local arch bundled
  arch="$(uname -m 2>/dev/null || true)"
  bundled="$(pwd)/runtime/darwin-${arch}/bin/python3"
  if [ -x "$bundled" ]; then
    # Slack/email zips often quarantine the nested interpreter.
    xattr -dr com.apple.quarantine "$(pwd)/runtime" 2>/dev/null || true
    echo "$bundled"
    return 0
  fi
  return 1
}

find_python() {
  local candidate
  if candidate="$(bundled_python)"; then
    echo "$candidate"
    return 0
  fi
  for candidate in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" 2>/dev/null; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

need_python() {
  PYTHON3="$(find_python)" && return 0
  echo ""
  echo "Python 3.9+ is required to run dCloud Content Manager."
  echo "You have the -update zip (no Python bundled)."
  echo "Either install Python from https://www.python.org/downloads/macos/"
  echo "or ask for the -full zip, which includes Python for Apple Silicon and Intel."
  echo ""
  read -r -p "Press Enter to close this window..."
  exit 1
}

capture_terminal_window() {
  case "${TERM_PROGRAM:-}" in
    Apple_Terminal)
      TERMINAL_WINDOW_ID=$(osascript -e 'tell application "Terminal" to id of front window' 2>/dev/null)
      ;;
    iTerm.app)
      ITERM_SESSION_ID="${ITERM_SESSION_ID:-}"
      ;;
  esac
}

close_terminal_window_by_id() {
  local win_id="$1"
  [ -z "$win_id" ] && return
  osascript -e "tell application \"Terminal\" to close window id ${win_id} saving no" 2>/dev/null || true
}

close_this_terminal_window() {
  case "${TERM_PROGRAM:-}" in
    Apple_Terminal)
      close_terminal_window_by_id "$TERMINAL_WINDOW_ID"
      ;;
    iTerm.app)
      if [ -n "${ITERM_SESSION_ID:-}" ]; then
        osascript -e "tell application \"iTerm\" to close session id \"${ITERM_SESSION_ID}\"" 2>/dev/null || true
      fi
      ;;
  esac
}

stop_previous_launcher() {
  [ ! -f "$SESSION_FILE" ] && return
  local old_pid="" old_win=""
  read -r old_pid old_win < "$SESSION_FILE" 2>/dev/null || true
  if [ -n "$old_pid" ] && [ "$old_pid" != "$$" ] && kill -0 "$old_pid" 2>/dev/null; then
    echo "Closing previous launcher window..."
    kill -TERM "$old_pid" 2>/dev/null || true
    sleep 0.4
  fi
  if [ -n "$old_win" ] && [ "$old_win" != "${TERMINAL_WINDOW_ID:-}" ]; then
    close_terminal_window_by_id "$old_win"
  fi
}

register_launcher() {
  echo "$$ ${TERMINAL_WINDOW_ID:-}" > "$SESSION_FILE"
}

stop_app() {
  [ -z "${APP_PID:-}" ] && return
  kill -0 "$APP_PID" 2>/dev/null || return
  # Reload mode runs a worker child, so stop the tree and not just the parent.
  pkill -TERM -P "$APP_PID" 2>/dev/null || true
  kill -TERM "$APP_PID" 2>/dev/null || true
}

cleanup_session() {
  if [ -f "$SESSION_FILE" ]; then
    local file_pid=""
    read -r file_pid _ < "$SESSION_FILE" 2>/dev/null || true
    if [ "$file_pid" = "$$" ]; then
      rm -f "$SESSION_FILE"
    fi
  fi
}

capture_terminal_window
stop_previous_launcher
register_launcher
trap 'stop_app; cleanup_session' EXIT
# Closing the window used to leave the server running and holding the port.
trap 'stop_app; cleanup_session; exit 143' TERM INT HUP

if [ "$(id -u)" = "0" ]; then
  echo ""
  echo "Do not run this with sudo."
  echo "sudo makes the .venv folder owned by root, and then normal runs fail."
  echo "Close this window and just double-click start.command."
  echo ""
  read -r -p "Press Enter to close this window..."
  exit 1
fi

# A previous sudo run leaves a root-owned .venv that this user cannot write to.
if [ -d ".venv" ] && [ ! -w ".venv" ]; then
  echo ""
  echo "The .venv folder in this project is not writable by you."
  echo "It was probably created by an earlier 'sudo ./start.command' run."
  echo "Fix it by running this once, then double-click start.command again:"
  echo "  sudo rm -rf \"$(pwd)/.venv\""
  echo ""
  read -r -p "Press Enter to close this window..."
  exit 1
fi

need_python
echo "Python: $PYTHON3"

# Zip installs (including the full Python bundle) and git clones both check
# GitHub here. Jobs, logins, .venv, and runtime/ are never replaced.
if [ "${DCLOUD_SKIP_UPDATE:-}" != "1" ] && [ -f "update_from_github.py" ]; then
  "$PYTHON3" update_from_github.py || true
fi

create_venv() {
  "$PYTHON3" -m venv .venv || {
    echo "Could not create .venv with: $PYTHON3"
    read -r -p "Press Enter to close this window..."
    exit 1
  }
}

if [ ! -d ".venv" ]; then
  echo "First run: creating a private environment in this folder (.venv)."
  echo "App packages stay here — nothing is installed for the rest of your Mac."
  create_venv
elif ! .venv/bin/python -c "import sys" >/dev/null 2>&1; then
  # A copied or moved folder leaves .venv pointing at a Python that is gone.
  echo "Rebuilding .venv (it pointed at a Python that no longer exists)..."
  rm -rf .venv
  create_venv
fi

echo "Checking Python packages..."
# python -m pip, not .venv/bin/pip, so a stale pip shebang cannot break the run.
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt || {
  echo "Package install failed. Check your network and try again."
  read -r -p "Press Enter to close this window..."
  exit 1
}

if [ -f "package.json" ] && command -v npm >/dev/null 2>&1; then
  if [ ! -d "static/vendor/harbor-elements" ]; then
    echo "Installing Atmosphere / Harbor UI packages (Cisco Artifactory, first run on this branch)..."
    npm install || echo "Atmosphere install failed — the tool still runs with the previous look."
  fi
fi

# Tool-owned Chromium for Cisco SSO (CAMGR/CAI/dCloud). Lives in this folder so a
# GitHub update does not require a new zip, and Chrome Keychain is never opened.
export PLAYWRIGHT_BROWSERS_PATH="$(pwd)/.playwright-browsers"
if ! .venv/bin/python -c "from playwright.sync_api import sync_playwright as S
p=S().start(); path=p.chromium.executable_path; p.stop(); raise SystemExit(0 if path else 1)" >/dev/null 2>&1; then
  echo "Downloading Chromium for sign-in (one time; this is not Google Chrome)..."
  .venv/bin/python -m playwright install chromium || echo "Chromium download failed — Connect to CAMGR/CAI can still try your Chrome tab."
fi

if [ ! -f ".env" ]; then
  if [ -f ".env.example" ]; then
    echo "Creating .env from .env.example (first run only)..."
    cp ".env.example" ".env"
  fi
fi

# Only the listener, never the browser tabs connected to it: a plain
# "lsof -ti :PORT" also lists Chrome's client sockets.
port_listeners() {
  lsof -ti :"$PORT" -sTCP:LISTEN 2>/dev/null
}

free_port() {
  local pids attempt
  pids="$(port_listeners)"
  [ -z "$pids" ] && return 0
  echo "Stopping previous server on port $PORT..."
  # A reload-mode server can outlive its launcher window and keep the port, so
  # stop its whole process tree and wait for the port to actually clear. The
  # parent goes first, or its reloader just starts a replacement worker.
  for pid in $pids; do
    kill -TERM "$pid" 2>/dev/null || true
    pkill -TERM -P "$pid" 2>/dev/null || true
  done
  for attempt in 1 2 3 4 5 6; do
    sleep 0.5
    [ -z "$(port_listeners)" ] && return 0
  done
  echo "Previous server did not exit, forcing it to stop..."
  for pid in $(port_listeners); do
    kill -KILL "$pid" 2>/dev/null || true
    pkill -KILL -P "$pid" 2>/dev/null || true
  done
  for attempt in 1 2 3 4 5 6; do
    sleep 0.5
    [ -z "$(port_listeners)" ] && return 0
  done
  return 1
}

if ! free_port; then
  echo ""
  echo "Port $PORT is still in use by another program:"
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null
  echo ""
  echo "Close that program (or restart your Mac) and run this again."
  read -r -p "Press Enter to close this window..."
  exit 1
fi

echo "Starting dCloud Content Manager..."
open "http://127.0.0.1:$PORT"
.venv/bin/python app.py &
APP_PID=$!
wait "$APP_PID"
code=$?
APP_PID=""

# 143/137 = killed when a new start.command replaces this launcher
case "$code" in
  0|130|143|137)
    close_this_terminal_window
    exit 0
    ;;
  *)
    echo ""
    echo "Server stopped (exit $code)."
    # Keep the window open: a crash message above is the only clue to what broke.
    echo "Scroll up to read the error. Send a screenshot of it if you need help."
    echo ""
    read -r -p "Press Enter to close this window..."
    exit "$code"
    ;;
esac
