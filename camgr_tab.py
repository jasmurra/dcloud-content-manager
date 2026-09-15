"""Talk to an already-open Google Chrome CAMGR tab (same live session, no extra profile)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

CAMGR_TAB_SESSION = "camgr-chrome-tab"
_CAMGR_HOST = "dcloud-camgr.cisco.com"
_CAMGR_HOME = "https://dcloud-camgr.cisco.com/#/cas"
# Chrome keeps a separate cookie jar per incognito window, so a CAMGR tab has to
# land in the same window as this tool's page to share that window's login.
_APP_URL_HINT = f"127.0.0.1:{os.getenv('PORT', '8768')}"
APPLE_EVENTS_JS_HINT = (
    "Chrome is blocking scripted access to your tabs. In Chrome: View → Developer → "
    "Allow JavaScript from Apple Events, then click Connect to CAMGR again."
)


def using_chrome_tab(cookie_header: str) -> bool:
    return str(cookie_header or "").strip() == CAMGR_TAB_SESSION


def _osascript(source: str, timeout: float = 40) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            ["osascript", "-"],
            input=source,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()


TAB_LOADING_HINT = "CAMGR is still loading in Chrome. Click Connect to CAMGR again in a moment."
NO_TAB_HINT = "No CAMGR tab in Chrome. Open CAMGR, then Connect."


def _camgr_tab_state() -> str:
    """Return ready for a loaded CAMGR tab, loading while it navigates, empty for none."""
    code, out, _err = _osascript(
        '''
tell application "Google Chrome"
  if it is not running then return ""
  set found to ""
  repeat with w in windows
    repeat with t in tabs of w
      try
        if (URL of t as text) contains "dcloud-camgr.cisco.com" then
          if loading of t then
            set found to "loading"
          else
            return "ready"
          end if
        end if
      end try
    end repeat
  end repeat
  return found
end tell
'''
    )
    if code != 0:
        return ""
    return out if out in {"ready", "loading"} else ""


def _chrome_has_camgr_tab() -> bool:
    return bool(_camgr_tab_state())


def _wait_for_camgr_tab(timeout: float = 15.0) -> str:
    """Wait for a CAMGR tab that has finished loading. Returns "" or a reason."""
    deadline = time.time() + timeout
    while True:
        state = _camgr_tab_state()
        if state == "ready":
            return ""
        if time.time() >= deadline:
            return TAB_LOADING_HINT if state else "Chrome did not open a CAMGR tab."
        time.sleep(1)


def _new_tab_beside_app() -> bool:
    """Open CAMGR in the window showing this tool, so an incognito login still applies."""
    code, out, _err = _osascript(
        f'''
tell application "Google Chrome"
  if it is not running then return "0"
  repeat with w in windows
    repeat with t in tabs of w
      try
        if (URL of t as text) contains "{_APP_URL_HINT}" then
          tell w to make new tab with properties {{URL:"{_CAMGR_HOME}"}}
          return "1"
        end if
      end try
    end repeat
  end repeat
end tell
return "0"
'''
    )
    return code == 0 and out == "1"


def _open_camgr_in_chrome() -> str:
    if _camgr_tab_state() == "ready":
        return ""
    if not _chrome_has_camgr_tab():
        if not _new_tab_beside_app():
            try:
                subprocess.run(
                    ["open", "-a", "Google Chrome", _CAMGR_HOME],
                    check=False,
                    capture_output=True,
                    timeout=15,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return str(exc)
    # A tab that just opened still has to load CAMGR (and any SSO redirect)
    # before a same-origin request can run inside it.
    return _wait_for_camgr_tab()


def _js_request(method: str, url: str, payload: Any | None) -> str:
    method = str(method or "GET").upper()
    if urlparse(url).hostname not in {_CAMGR_HOST, None} and _CAMGR_HOST not in url:
        raise ValueError("CAMGR tab requests must stay on dcloud-camgr.cisco.com")
    body = json.dumps(payload) if payload is not None and method != "GET" else None
    return f"""
(function() {{
  var xhr = new XMLHttpRequest();
  xhr.open({json.dumps(method)}, {json.dumps(url)}, false);
  xhr.setRequestHeader("Accept", "application/json");
  try {{
    {f"xhr.setRequestHeader('Content-Type', 'application/json'); xhr.send({json.dumps(body)});" if body is not None else "xhr.send(null);"}
  }} catch (err) {{
    return JSON.stringify({{status: 0, url: "", body: "", error: String(err)}});
  }}
  return JSON.stringify({{
    status: xhr.status,
    url: xhr.responseURL || "",
    body: xhr.responseText || ""
  }});
}})();
"""


def _run_js_in_camgr_tab(js: str) -> tuple[str, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(js)
        js_path = Path(handle.name)
    posix = str(js_path).replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
set js to read POSIX file "{posix}" as «class utf8»
tell application "Google Chrome"
  if it is not running then return ""
  repeat with w in windows
    repeat with t in tabs of w
      try
        if (URL of t as text) contains "dcloud-camgr.cisco.com" then
          return execute javascript js in t
        end if
      end try
    end repeat
  end repeat
end tell
return ""
'''
    try:
        code, out, err = _osascript(script, timeout=45)
    finally:
        js_path.unlink(missing_ok=True)
    if code != 0:
        # -1723 is Chrome refusing Apple Events JavaScript, which is off by default.
        if "-1723" in err or "Access not allowed" in err:
            return "", APPLE_EVENTS_JS_HINT
        return "", err or "Could not control Chrome. Allow this app to control Google Chrome, then try Connect again."
    return out, err


def chrome_tab_request(
    method: str,
    url: str,
    payload: Any | None = None,
    *,
    open_if_missing: bool = False,
) -> tuple[int, Any, str]:
    """Run a same-origin XHR inside a live CAMGR Chrome tab and return status, JSON, error."""
    if open_if_missing:
        opened = _open_camgr_in_chrome()
        if opened and not _chrome_has_camgr_tab():
            return 0, None, opened
    elif not _chrome_has_camgr_tab():
        return 0, None, NO_TAB_HINT
    js = _js_request(method, url, payload)
    raw, err = _run_js_in_camgr_tab(js)
    # An empty result with no error means the tab was mid-navigation.
    for _ in range(2):
        if raw or err:
            break
        time.sleep(1.5)
        raw, err = _run_js_in_camgr_tab(js)
    if not raw:
        if err:
            return 0, None, err
        return 0, None, TAB_LOADING_HINT if _chrome_has_camgr_tab() else NO_TAB_HINT
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return 0, None, "Chrome did not return a CAMGR response."
    status = int(data.get("status") or 0)
    final_url = str(data.get("url") or "").lower()
    body_text = str(data.get("body") or "")
    js_error = str(data.get("error") or "")
    if js_error:
        return 0, None, js_error
    if any(part in final_url for part in ("duosecurity", "id.cisco.com", "cloudsso", "login.cisco.com")):
        return status, None, "CAMGR tab is not signed in."
    parsed: Any = None
    text = body_text.strip()
    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
    return status, parsed, ""


def probe_camgr_via_chrome_tab(*, open_if_missing: bool = False) -> dict[str, Any]:
    status, body, err = chrome_tab_request(
        "GET",
        "https://dcloud-camgr.cisco.com/ca/api/users/current",
        open_if_missing=open_if_missing,
    )
    if err:
        return {"ok": False, "loggedIn": False, "message": err}
    user = ""
    if isinstance(body, dict):
        for key in ("id", "username", "user", "userId"):
            user = str(body.get(key) or "").strip()
            if user:
                break
    if status >= 400 or not user:
        return {
            "ok": False,
            "loggedIn": False,
            "message": "CAMGR tab is open but not signed in.",
        }
    return {
        "ok": True,
        "loggedIn": True,
        "user": user,
        "cookie": CAMGR_TAB_SESSION,
        "message": f"CAMGR session is active ({user}).",
    }


def connect_camgr_via_chrome_tab() -> tuple[str | None, str]:
    probed = probe_camgr_via_chrome_tab(open_if_missing=True)
    # A tab opened by this click may still be finishing its SSO redirect, so give
    # it a few seconds instead of telling the user to click Connect again.
    deadline = time.time() + 10
    while not probed.get("loggedIn") and time.time() < deadline:
        message = str(probed.get("message") or "")
        if message not in {TAB_LOADING_HINT, NO_TAB_HINT} and "not signed in" not in message.lower():
            break
        time.sleep(2)
        probed = probe_camgr_via_chrome_tab()
    if probed.get("loggedIn"):
        return CAMGR_TAB_SESSION, str(probed.get("message") or "CAMGR session is active.")
    return None, str(probed.get("message") or "Could not use the open CAMGR tab.")
