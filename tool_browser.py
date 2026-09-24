"""A tool-owned Chromium profile for Cisco SSO — no Chrome cookie DB, no Keychain.

Playwright's bundled Chromium keeps its own cookies under .dcloud-tool-chrome/.
Sign in once (Duo included); later CAMGR/CAI/dCloud visits reuse that SSO session.
Refresh happens by loading the site in this profile, not by decrypting Chrome Safe Storage.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

APP_DIR = Path(__file__).resolve().parent
PROFILE_DIR = APP_DIR / ".dcloud-tool-chrome"
BROWSERS_DIR = APP_DIR / ".playwright-browsers"
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BROWSERS_DIR))

_launch_lock = threading.Lock()
_playwright_ready = False

# Hosts that mean "a person has to type something": Cisco's Okta tenant and Duo.
IDP_HOSTS = ("id.cisco.com", "login.okta.com", "duosecurity.com", "cloudsso.cisco.com")
IDP_SETTLE_SECONDS = 6.0
DCLOUD_SIGN_IN_NEEDED = (
    "dCloud needs a sign-in in the tool browser — click Log in to dCloud."
)


def _is_idp_page(page: Any) -> bool:
    """True when the tool browser is parked on a login/Duo page awaiting a human."""
    try:
        host = (urlparse(page.url or "").hostname or "").lower()
    except Exception:
        return False
    return any(host == idp or host.endswith(f".{idp}") or idp in host for idp in IDP_HOSTS)


def profile_exists() -> bool:
    return (PROFILE_DIR / "Default").is_dir() or (PROFILE_DIR / "Cookies").is_file()


def _chromium_on_disk() -> bool:
    """True when Playwright Chromium is already downloaded into this install."""
    if not BROWSERS_DIR.is_dir():
        return False
    names = {"Chromium", "chrome", "chrome.exe"}
    for path in BROWSERS_DIR.rglob("*"):
        if path.name in names and path.is_file():
            return True
    return False


def _install_playwright_package() -> str | None:
    """Install the Playwright Python package into this app's venv."""
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-q",
                "playwright>=1.49.0",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return (
            "Playwright is not installed yet. Quit and double-click start.command "
            f"so it can download Chromium (one time). ({exc})"
        )
    return None


def ensure_playwright() -> str | None:
    """Install Playwright's Chromium once if needed. Returns an error or None."""
    global _playwright_ready
    if _playwright_ready:
        return None
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        # Check for updates copies new code without pip. Connect should finish
        # that install instead of asking for a restart and extra clicks.
        err = _install_playwright_package()
        if err:
            return err
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            return (
                "Playwright is not installed yet. Quit and double-click start.command "
                "so it can download Chromium (one time)."
            )
    # Do not start the Playwright driver just to ask where Chromium is — that
    # adds several seconds before the sign-in window can open.
    if _chromium_on_disk():
        _playwright_ready = True
        return None
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(BROWSERS_DIR)},
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return f"Could not download Chromium for sign-in ({exc})."
    _playwright_ready = True
    return None


def _cookie_header(raw: list[dict[str, Any]], hosts: tuple[str, ...]) -> str:
    cookies: dict[str, str] = {}
    for item in raw:
        domain = str(item.get("domain") or "").lstrip(".").lower()
        if not any(host in domain for host in hosts):
            continue
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        if name and value:
            cookies[name] = value
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def _launch(playwright: Any, *, headed: bool):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=not headed,
        viewport={"width": 1200, "height": 860},
        ignore_default_args=["--enable-automation"],
        args=["--disable-blink-features=AutomationControlled"],
    )


def capture_site_cookies(
    url: str,
    hosts: tuple[str, ...],
    is_logged_in: Callable[[str], bool],
    *,
    headed: bool = True,
    timeout_s: float = 180,
) -> tuple[str | None, str]:
    """Open url in the tool Chromium profile and return a Cookie header once signed in."""
    missing = ensure_playwright()
    if missing:
        return None, missing
    from playwright.sync_api import sync_playwright

    deadline = time.time() + max(20.0, timeout_s)
    with _launch_lock:
        with sync_playwright() as playwright:
            try:
                context = _launch(playwright, headed=headed)
            except Exception as exc:
                return None, f"Could not open the sign-in browser ({exc})."
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                last_header = ""
                started = time.time()
                while time.time() < deadline:
                    if headed and not context.pages:
                        return None, "The sign-in window was closed before login finished."
                    last_header = _cookie_header(context.cookies(), hosts)
                    if last_header and is_logged_in(last_header):
                        return last_header, "Signed in with the tool browser."
                    # Silent callers gain nothing by waiting out a login form.
                    if (
                        not headed
                        and time.time() - started > IDP_SETTLE_SECONDS
                        and _is_idp_page(page)
                    ):
                        return None, "A sign-in is needed in the tool browser."
                    time.sleep(1.2)
                if last_header and is_logged_in(last_header):
                    return last_header, "Signed in with the tool browser."
                return None, (
                    "Timed out waiting for Cisco SSO in the tool browser. "
                    "Finish Duo if a prompt is showing, then click Connect again."
                )
            finally:
                try:
                    context.close()
                except Exception:
                    pass


def capture_dcloud_tokens(
    site: str = "rtp",
    *,
    headed: bool = True,
    timeout_s: float = 180,
) -> tuple[str, str, str, str]:
    """Return (access, refresh, site, message) from the tool Chromium dCloud session."""
    missing = ensure_playwright()
    if missing:
        return "", "", site, missing
    from playwright.sync_api import sync_playwright

    from browser_auth.dcloud_oauth import build_dcloud_login_url, exchange_dcloud_access_code

    site_code = (site or "rtp").strip().lower() or "rtp"
    # Start at the Cisco SSO authorize URL rather than the dCloud home page: an
    # anonymous hit on dcloud2-*.cisco.com just redirects to the public marketing
    # site, which never sets any token. Authorize redirects to /authenticate?code=...
    # and an SSO session already in this profile makes that hop silent.
    login_url, _state = build_dcloud_login_url(site_code)
    deadline = time.time() + max(20.0, timeout_s)
    with _launch_lock:
        with sync_playwright() as playwright:
            try:
                context = _launch(playwright, headed=headed)
            except Exception as exc:
                return "", "", site_code, f"Could not open the sign-in browser ({exc})."
            try:
                # The dCloud app consumes the code and navigates on within a few
                # hundred ms, so watch navigations instead of only sampling page.url.
                seen: list[str] = []

                def note(url: str) -> None:
                    code = _code_from_url(url)
                    if code and code not in seen:
                        seen.append(code)

                def attach(target: Any) -> None:
                    target.on("framenavigated", lambda frame: note(frame.url or ""))

                page = context.pages[0] if context.pages else context.new_page()
                attach(page)
                context.on("page", attach)
                page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)
                last_err = "Waiting for dCloud sign-in in the tool browser."
                started = time.time()
                while time.time() < deadline:
                    if headed and not context.pages:
                        return "", "", site_code, "The sign-in window was closed before login finished."
                    # Silent callers must not sit here: once the redirects settle on an
                    # identity-provider page, only a real person can move it forward.
                    if (
                        not headed
                        and time.time() - started > IDP_SETTLE_SECONDS
                        and _is_idp_page(page)
                    ):
                        return "", "", site_code, DCLOUD_SIGN_IN_NEEDED
                    for code in (*seen, _oauth_code_from_pages(context)):
                        if not code:
                            continue
                        access, refresh, _expires, err = exchange_dcloud_access_code(site_code, code)
                        if access:
                            return access, refresh, site_code, "Signed in with the tool browser."
                        last_err = err or last_err
                    seen.clear()
                    # The app may also have exchanged the code itself by now.
                    access, refresh, found_site = _read_dcloud_storage(page, site_code)
                    if access:
                        return access, refresh, found_site, "Signed in with the tool browser."
                    time.sleep(0.6)
                return "", "", site_code, last_err
            finally:
                try:
                    context.close()
                except Exception:
                    pass


def _code_from_url(url: str) -> str:
    try:
        parsed = urlparse(url or "")
    except Exception:
        return ""
    if "dcloud2-" not in (parsed.hostname or "") or "authenticate" not in parsed.path:
        return ""
    return (parse_qs(parsed.query).get("code") or [""])[0].strip()


def _oauth_code_from_pages(context: Any) -> str:
    for page in list(context.pages):
        try:
            code = _code_from_url(page.url or "")
        except Exception:
            continue
        if code:
            return code
    return ""


def _read_dcloud_storage(page: Any, site: str) -> tuple[str, str, str]:
    try:
        values = page.evaluate(
            """() => ({
              access: localStorage.getItem('dc_p_a') || '',
              refresh: localStorage.getItem('dc_p_r') || ''
            })"""
        )
    except Exception:
        return "", "", site
    access = str((values or {}).get("access") or "").strip()
    refresh = str((values or {}).get("refresh") or "").strip()
    host = ""
    try:
        host = urlparse(page.url or "").hostname or ""
    except Exception:
        host = ""
    found = site
    for code in ("rtp", "sjc", "lon", "sng", "syd"):
        if f"dcloud2-{code}." in host:
            found = code
            break
    return access, refresh, found
