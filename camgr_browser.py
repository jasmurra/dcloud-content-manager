"""Sign in to CAMGR in the tool-owned Chromium profile (no Chrome Keychain)."""

from __future__ import annotations

from pathlib import Path

from camgr_client import CAMGR_BASE, CAMGR_HOME, probe_camgr_login
from tool_browser import PROFILE_DIR, capture_site_cookies, profile_exists


def capture_camgr_session(
    profile_dir: Path | None = None,
    timeout_s: float = 180,
    *,
    headed: bool | None = None,
) -> tuple[str | None, str]:
    """Wait until CAMGR is signed in inside the tool browser and return Cookie header."""
    del profile_dir  # Kept so older callers still type-check; profile is shared.
    hosts = ("dcloud-camgr.cisco.com",)
    silent = headed is False or (headed is None and profile_exists())
    if silent:
        header, message = capture_site_cookies(
            CAMGR_HOME,
            hosts,
            lambda cookie: bool(probe_camgr_login(cookie, allow_tab=False).get("loggedIn")),
            headed=False,
            timeout_s=min(25.0, timeout_s),
        )
        if header:
            return header, message
        if headed is False:
            return None, message
    return capture_site_cookies(
        CAMGR_HOME,
        hosts,
        lambda cookie: bool(probe_camgr_login(cookie, allow_tab=False).get("loggedIn")),
        headed=True,
        timeout_s=timeout_s,
    )


def camgr_profile_dir() -> Path:
    return PROFILE_DIR
