#!/bin/bash
cd "$(dirname "$0")"

PORT=8768
SESSION_FILE="/tmp/dcloud-content-manager-${PORT}.session"

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
trap cleanup_session EXIT

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment (first run only)..."
  python3 -m venv .venv
fi

.venv/bin/pip install -q -r requirements.txt

if [ ! -f ".env" ]; then
  if [ -f ".env.example" ]; then
    echo "Creating .env from .env.example (first run only)..."
    cp ".env.example" ".env"
  fi
fi

if lsof -ti :"$PORT" >/dev/null 2>&1; then
  echo "Stopping previous server on port $PORT..."
  lsof -ti :"$PORT" | xargs kill 2>/dev/null || true
  sleep 1
fi

echo "Starting dCloud Content Manager..."
open "http://127.0.0.1:$PORT"
.venv/bin/python app.py
code=$?

# 143/137 = killed when a new start.command replaces this launcher
case "$code" in
  0|130|143|137)
    close_this_terminal_window
    exit 0
    ;;
  *)
    echo "Server stopped (exit $code)."
    sleep 3
    exit "$code"
    ;;
esac
