#!/bin/bash
cd "$(dirname "$0")"

export PATH="/opt/homebrew/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/Current/bin:$PATH"

TERMINAL_WINDOW_ID=""
if [ "${TERM_PROGRAM:-}" = "Apple_Terminal" ]; then
  TERMINAL_WINDOW_ID=$(osascript -e 'tell application "Terminal" to id of front window' 2>/dev/null)
fi

close_this_window() {
  case "${TERM_PROGRAM:-}" in
    Apple_Terminal)
      [ -n "$TERMINAL_WINDOW_ID" ] && osascript -e "tell application \"Terminal\" to close window id ${TERMINAL_WINDOW_ID} saving no" 2>/dev/null || true
      ;;
    iTerm.app)
      [ -n "${ITERM_SESSION_ID:-}" ] && osascript -e "tell application \"iTerm\" to close session id \"${ITERM_SESSION_ID}\"" 2>/dev/null || true
      ;;
  esac
}

if ! python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" 2>/dev/null; then
  echo "Python 3.9+ is required on this Mac to build the coworker zip."
  echo "Install it from https://www.python.org/downloads/macos/"
  read -r -p "Press Enter to close this window..."
  exit 1
fi

echo "Building the larger Mac zip with Python bundled (Apple Silicon + Intel)."
echo "First pack downloads Python into a local cache (once). Coworkers do not download it."
echo "Version is not bumped — run share-for-mac.command first if this is a new drop."
python3 pack_for_mac.py --with-python
code=$?
if [ "$code" -ne 0 ]; then
  echo "Pack failed (exit $code)."
  read -r -p "Press Enter to close this window..."
  exit "$code"
fi

echo ""
echo "Done. Finder should be highlighting dCloud-Content-Manager-Mac-with-Python.zip"
echo "The smaller dCloud-Content-Manager-Mac.zip is unchanged."
sleep 1
close_this_window
exit 0
