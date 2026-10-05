"""A tool-owned Chromium profile for Cisco SSO — no Chrome cookie DB, no Keychain.

Playwright's bundled Chromium keeps its own cookies under .dcloud-tool-chrome/.
Sign in once (Duo included); later CAMGR/CAI/dCloud visits reuse that SSO session.
Refresh happens by loading the site in this profile, not by decrypting Chrome Safe Storage.
"""

from __future__ import annotations

import json
import os
import platform
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from browser_auth.dcloud_token import jwt_expires_at

APP_DIR = Path(__file__).resolve().parent
PROFILE_DIR = APP_DIR / ".dcloud-tool-chrome"
BROWSERS_DIR = APP_DIR / ".playwright-browsers"
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BROWSERS_DIR))

_launch_lock = threading.Lock()
# Set while a person is waiting for the visible sign-in window, so a background
# refresh lets go of the profile instead of hiding that window.
_user_needs_window = threading.Event()
_headed_open_lock = threading.Lock()
_headed_open = False
_playwright_ready = False
_hub_cookies_lock = threading.Lock()
_hub_cookies: dict[str, str] = {}

# Same hosts the Connect buttons visit, so one Log in window can leave CAI and
# CAMGR signed in without extra clicks.
HUB_SITES = (
    ("cai", "https://dcloud-cai.cisco.com/", ("dcloud-cai.cisco.com",)),
    ("camgr", "https://dcloud-camgr.cisco.com/#/cas", ("dcloud-camgr.cisco.com",)),
)

# Hosts that mean "a person has to type something": Cisco's Okta tenant and Duo.
IDP_HOSTS = (
    "id.cisco.com",
    "login.cisco.com",
    "sso.cisco.com",
    "login.okta.com",
    "duosecurity.com",
    "cloudsso.cisco.com",
)
IDP_SETTLE_SECONDS = 6.0
# A saved dCloud token can sit in this profile from last time. Give Cisco a
# moment to redirect onto Duo before that old token counts as "already signed in".
SSO_SETTLE_SECONDS = 8.0
# Duo closes its popup for a moment when Cisco login finishes. That is not the
# user closing the sign-in window. "Continue in browser" can also take the
# person to another window and back, so a short gap is not them quitting.
WINDOW_BLANK_GRACE_SECONDS = 45.0
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


# Duo compares the sign-in browser with current Chrome Stable. Playwright's
# copy only changes when Playwright is released, so a build one version behind
# (Chrome 153 while Stable is 154) is refused as "Chrome update required".
CFT_VERSIONS_URL = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "last-known-good-versions-with-downloads.json"
)
CFT_DOWNLOAD_HOST = "storage.googleapis.com"
CFT_DOWNLOAD_PREFIX = "/chrome-for-testing-public/"
CFT_DIR = BROWSERS_DIR / "chrome-for-testing"
CFT_STATE_FILE = CFT_DIR / "last-check.json"
# Ask Google which Stable build is current at most this often, once we already
# have that build. A browser we know is behind is retried on the next sign-in.
CFT_CHECK_SECONDS = 12 * 60 * 60
CFT_FAIL_BACKOFF_SECONDS = 10 * 60


def _version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(text or "").split("."):
        digits = ""
        for char in piece:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _cft_platform() -> str | None:
    machine = platform.machine().lower()
    arm = machine in {"arm64", "aarch64"}
    system = platform.system()
    if system == "Darwin":
        return "mac-arm64" if arm else "mac-x64"
    if system == "Linux":
        return "linux-arm64" if arm else "linux64"
    if system == "Windows":
        return "win64" if machine in {"amd64", "x86_64"} else "win32"
    return None


def _trusted_cft_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == CFT_DOWNLOAD_HOST
        and parsed.path.startswith(CFT_DOWNLOAD_PREFIX)
    )


def _stable_chrome_download(catalog: dict[str, Any]) -> tuple[str, str] | None:
    """Stable version and this machine's Chrome for Testing zip, or None."""
    stable = (catalog.get("channels") or {}).get("Stable") or {}
    version = str(stable.get("version") or "").strip()
    platform_name = _cft_platform()
    if not version or not platform_name:
        return None
    for item in (stable.get("downloads") or {}).get("chrome") or []:
        url = str(item.get("url") or "")
        if item.get("platform") == platform_name and _trusted_cft_url(url):
            return version, url
    return None


def _chrome_app_version(app: Path) -> str:
    plist_path = app / "Contents" / "Info.plist"
    if not plist_path.is_file():
        return ""
    try:
        with plist_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return ""
    return str(info.get("CFBundleShortVersionString") or "").strip()


def _chrome_testing_apps() -> list[Path]:
    if not BROWSERS_DIR.is_dir():
        return []
    found: list[Path] = []
    for pattern in (
        "chrome-for-testing/**/Google Chrome for Testing.app",
        "chromium-*/chrome-mac*/Google Chrome for Testing.app",
    ):
        found.extend(path for path in BROWSERS_DIR.glob(pattern) if path.is_dir())
    return found


def _tool_chrome_app() -> Path | None:
    """The sign-in app is Google Chrome for Testing, newest copy we have."""
    apps = _chrome_testing_apps()
    if not apps:
        return None
    return max(apps, key=lambda app: _version_tuple(_chrome_app_version(app)))


def _sign_in_chrome_executable() -> str | None:
    app = _tool_chrome_app()
    if app is None:
        return None
    binary = app / "Contents" / "MacOS" / "Google Chrome for Testing"
    if binary.is_file():
        return str(binary)
    return None


def _local_sign_in_chrome_version() -> str:
    app = _tool_chrome_app()
    return _chrome_app_version(app) if app else ""


def _read_cft_state() -> dict[str, Any]:
    try:
        data = json.loads(CFT_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cft_state(**fields: Any) -> None:
    CFT_DIR.mkdir(parents=True, exist_ok=True)
    state = _read_cft_state()
    state.update(fields)
    CFT_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def _fetch_cft_catalog() -> tuple[dict[str, Any] | None, str | None]:
    request = urllib.request.Request(CFT_VERSIONS_URL, headers={"User-Agent": "dcloud-content-manager"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return None, f"Could not check the current Chrome version ({exc})."
    if not isinstance(payload, dict):
        return None, "Chrome version list was not usable."
    return payload, None


def _safe_extract(archive: zipfile.ZipFile, dest: Path) -> None:
    root = dest.resolve()
    for member in archive.infolist():
        target = (dest / member.filename).resolve()
        if target != root and root not in target.parents:
            raise RuntimeError("Chrome download had an unexpected path.")
        archive.extract(member, dest)
        # ZipInfo keeps the executable bit in the upper half of external_attr.
        # extract() drops it, and Duo's browser then cannot be started.
        mode = (member.external_attr >> 16) & 0o777
        if mode and target.is_file():
            os.chmod(target, mode)


def _install_stable_chrome(version: str, url: str) -> None:
    """Download Chrome for Testing Stable into this install."""
    dest = CFT_DIR / version
    if any(_chrome_app_version(app) == version for app in _chrome_testing_apps()):
        return
    CFT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="chrome-for-testing-") as tmp:
        zip_path = Path(tmp) / "chrome.zip"
        request = urllib.request.Request(url, headers={"User-Agent": "dcloud-content-manager"})
        with urllib.request.urlopen(request, timeout=120) as response, zip_path.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        staging = Path(tmp) / "unpack"
        staging.mkdir()
        with zipfile.ZipFile(zip_path) as archive:
            _safe_extract(archive, staging)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(staging), str(dest))
    app = next((path for path in dest.glob("**/Google Chrome for Testing.app") if path.is_dir()), None)
    if app is None:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError("Chrome download had no browser app.")
    if platform.system() == "Darwin":
        subprocess.run(
            ["xattr", "-dr", "com.apple.quarantine", str(app)],
            check=False,
            capture_output=True,
        )
    for old in CFT_DIR.iterdir():
        if old.name in {version, CFT_STATE_FILE.name}:
            continue
        if old.is_dir():
            shutil.rmtree(old, ignore_errors=True)


def ensure_sign_in_chrome() -> str | None:
    """Download Chrome Stable when the sign-in browser has fallen behind.

    Returns an error string when the update fails. The caller still uses the
    browser already on disk when there is one.
    """
    installed = _local_sign_in_chrome_version()
    state = _read_cft_state()
    known_stable = str(state.get("stable") or "")
    behind = bool(known_stable) and _version_tuple(installed) < _version_tuple(known_stable)
    checked_at = float(state.get("checked_at") or 0)
    failed_at = float(state.get("failed_at") or 0)
    if installed and not behind and time.time() - checked_at < CFT_CHECK_SECONDS:
        return None
    if behind and time.time() - failed_at < CFT_FAIL_BACKOFF_SECONDS:
        return None
    catalog, err = _fetch_cft_catalog()
    if catalog is None:
        return err if behind or not installed else None
    found = _stable_chrome_download(catalog)
    if not found:
        return "Chrome Stable has no download for this Mac." if not installed else None
    version, url = found
    _write_cft_state(stable=version, checked_at=time.time(), installed=installed)
    if installed and _version_tuple(installed) >= _version_tuple(version):
        return None
    print(f"Updating the sign-in browser to Chrome {version} so Duo will accept it...", flush=True)
    try:
        _install_stable_chrome(version, url)
    except Exception as exc:
        _write_cft_state(failed_at=time.time())
        return f"Could not update the sign-in browser to Chrome {version} ({exc})."
    _write_cft_state(installed=version, failed_at=0)
    print(f"Sign-in browser is Chrome {version}.", flush=True)
    return None


def _chrome_brand_script() -> str:
    """Duo looks up the Google Chrome name and treats a reduced version as old.

    Chrome for Testing reports its real build only under the Chromium name, and
    the ordinary user agent is frozen at '154.0.0.0'. Copy the real version
    onto the Google Chrome name so Duo sees the build that is actually running.
    """
    return """
(() => {
  const native = navigator.userAgentData;
  if (!native || typeof native.getHighEntropyValues !== "function") return;
  const withBrand = (list, item) => {
    const brands = Array.isArray(list) ? list.slice() : [];
    if (!brands.some((brand) => brand && brand.brand === "Google Chrome")) brands.unshift(item);
    return brands;
  };
  const data = {
    get brands() {
      const major = (native.brands || []).find((brand) => brand && brand.brand === "Chromium");
      return withBrand(native.brands, { brand: "Google Chrome", version: major ? major.version : "" });
    },
    get mobile() { return native.mobile; },
    get platform() { return native.platform; },
    toJSON() { return { brands: this.brands, mobile: this.mobile, platform: this.platform }; },
    getHighEntropyValues(hints) {
      const wanted = Array.isArray(hints) ? hints.slice() : [];
      for (const hint of ["uaFullVersion", "fullVersionList"]) {
        if (!wanted.includes(hint)) wanted.push(hint);
      }
      return native.getHighEntropyValues(wanted).then((values) => {
        const full = String(values.uaFullVersion || "");
        const out = Object.assign({}, values);
        out.brands = withBrand(values.brands, { brand: "Google Chrome", version: full.split(".")[0] || "" });
        out.fullVersionList = withBrand(values.fullVersionList, { brand: "Google Chrome", version: full });
        return out;
      });
    },
  };
  try {
    Object.defineProperty(Navigator.prototype, "userAgentData", { configurable: true, get: () => data });
  } catch (error) {}
})();
"""


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


def _install_playwright_chromium() -> str | None:
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
    return None


def ensure_playwright() -> str | None:
    """Install Playwright and a current Chrome for Testing. Returns an error or None."""
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
                "so it can download the sign-in browser."
            )
    # Do not start the Playwright driver just to ask where Chromium is — that
    # adds several seconds before the sign-in window can open.
    fallback_err = None
    if not _chromium_on_disk() and _sign_in_chrome_executable() is None:
        fallback_err = _install_playwright_chromium()
    message = ensure_sign_in_chrome()
    if _sign_in_chrome_executable() or _chromium_on_disk():
        _playwright_ready = True
        return None
    return message or fallback_err or "Could not download the sign-in browser."


def _store_hub_cookies(cookies: dict[str, str]) -> None:
    with _hub_cookies_lock:
        _hub_cookies.clear()
        _hub_cookies.update({key: value for key, value in cookies.items() if value})


def take_hub_cookies() -> dict[str, str]:
    """Return CAI/CAMGR cookies captured during the last headed dCloud login."""
    with _hub_cookies_lock:
        cookies = dict(_hub_cookies)
        _hub_cookies.clear()
        return cookies


def _safe_pages(context: Any) -> list[Any]:
    try:
        return list(context.pages)
    except Exception:
        return []


def _page_on_hosts(page: Any, hosts: tuple[str, ...]) -> bool:
    try:
        host = (urlparse(page.url or "").hostname or "").lower()
    except Exception:
        return False
    return any(wanted in host for wanted in hosts)


def _still_on_idp(context: Any) -> bool:
    """True while any tool-browser tab is still the Cisco / Duo prompt."""
    return any(_is_idp_page(page) for page in _safe_pages(context))


def _open_target_page(context: Any, url: str) -> Any | None:
    """Open the site we still need a cookie from. Used after SSO, when the login popup has closed."""
    if _still_on_idp(context):
        # Do not navigate away from Duo. The person still has to click
        # Continue in browser, then come back to this same window.
        return None
    try:
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    except Exception:
        return None
    return page


def _duo_page(context: Any) -> Any | None:
    """The Duo card CAMGR opens after dCloud already signed in."""
    for page in _safe_pages(context):
        try:
            host = (urlparse(page.url or "").hostname or "").lower()
        except Exception:
            continue
        if "duosecurity" in host:
            return page
    return None


def _advance_duo_prompt(page: Any) -> bool:
    """Press the Duo button on CAMGR's redirect. The sign-in just finished."""
    try:
        clicked = page.evaluate(
            """() => {
              const nodes = [...document.querySelectorAll('button, a, input[type="submit"], [role="button"]')];
              const wanted = nodes.map(el => ({
                el,
                text: (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim().toLowerCase()
              }));
              const pick = wanted.find(item => item.text === 'log in')
                || wanted.find(item => item.text === 'continue' || item.text.startsWith('continue'));
              if (!pick) return '';
              pick.el.click();
              return pick.text;
            }"""
        )
    except Exception:
        return False
    return bool(clicked)


def _warm_hub_sessions(
    page: Any,
    context: Any,
    *,
    deadline: float | None = None,
) -> dict[str, str]:
    """Visit CAI and CAMGR in this same window once Duo is finished.

    The Duo "Continue in browser" page has to stay up until the person clicks
    it. Navigating to CAMGR, or closing the window, before that click loses
    the prompt and never stores a CAMGR cookie.
    """
    end = deadline if deadline is not None else time.time() + 240
    found: dict[str, str] = {}
    pending = sorted(HUB_SITES, key=lambda item: item[0] != "camgr")
    attempt_until = 0.0
    navigate_after = 0.0
    on_host_since: dict[str, float] = {}
    duo_clicks = 0
    last_duo_click = 0.0
    while pending and time.time() < end:
        workable = _work_page(context)
        if _still_on_idp(context) and workable is None:
            # CAMGR's own redirect lands here right after dCloud. Press Log in
            # once so the session they just finished is reused.
            now = time.time()
            duo = _duo_page(context)
            if duo is not None and duo_clicks < 3 and now - last_duo_click > 2:
                if _advance_duo_prompt(duo):
                    duo_clicks += 1
                    last_duo_click = now
                    time.sleep(1.0)
                    continue
            # Do not navigate away from Duo. A leftover Duo tab is not the
            # page CAMGR loads in, and closing it restarts the window.
            attempt_until = 0.0
            on_host_since.clear()
            time.sleep(0.6)
            continue
        key, url, hosts = pending[0]
        pages = _safe_pages(context)
        if workable is None and not pages:
            on_host_since.pop(key, None)
            if time.time() < navigate_after:
                time.sleep(0.6)
                continue
            navigate_after = time.time() + 5
            page = _open_target_page(context, url)
            if page is None:
                time.sleep(0.6)
                continue
        else:
            page = workable or (pages[-1] if pages else None)
            if page is None or _is_idp_page(page):
                on_host_since.clear()
                time.sleep(0.6)
                continue
            if not _page_on_hosts(page, hosts):
                on_host_since.pop(key, None)
                if time.time() < navigate_after:
                    time.sleep(0.4)
                    continue
                navigate_after = time.time() + 5
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                except Exception:
                    if _is_idp_page(page):
                        continue
                attempt_until = time.time() + 25
                continue
        if _is_idp_page(page):
            on_host_since.clear()
            continue
        # An old cookie can already be in the profile. Wait until this tab has
        # actually stayed on the site, so a hop through Duo is not counted.
        arrived = on_host_since.setdefault(key, time.time())
        if time.time() - arrived < 2.0:
            time.sleep(0.4)
            continue
        header = _cookie_header(context.cookies(), hosts)
        if header and (key != "camgr" or _camgr_cookie_works(header)):
            found[key] = header
            pending.pop(0)
            attempt_until = 0.0
            on_host_since.pop(key, None)
            continue
        # CAMGR is the cookie this sign-in exists to capture. Other hub
        # sites can be skipped after a short try; CAMGR waits out the deadline.
        if attempt_until <= 0:
            attempt_until = time.time() + 25
        if key != "camgr" and time.time() > attempt_until:
            pending.pop(0)
            attempt_until = 0.0
            continue
        time.sleep(0.6)
    return found


def _camgr_cookie_works(header: str) -> bool:
    """True only when CAMGR itself accepts the cookie. A leftover cookie must not close the window."""
    if not header:
        return False
    try:
        from camgr_client import probe_camgr_login

        return bool(probe_camgr_login(header, allow_tab=False, timeout=8).get("loggedIn"))
    except Exception:
        return False


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


def request_sign_in_window() -> None:
    """Ask a background browser to close so a visible sign-in window can open."""
    _user_needs_window.set()


def headed_is_open() -> bool:
    with _headed_open_lock:
        return _headed_open


def _set_headed_open(value: bool) -> None:
    global _headed_open
    with _headed_open_lock:
        _headed_open = value


class _BrowserTurn:
    """One Chromium profile at a time. A visible sign-in preempts a silent refresh."""

    def __init__(self, headed: bool):
        self.headed = headed
        self.held = False

    def __enter__(self) -> "_BrowserTurn":
        if self.headed:
            _user_needs_window.set()
            try:
                _launch_lock.acquire()
            except BaseException:
                _user_needs_window.clear()
                raise
            self.held = True
            _user_needs_window.clear()
            return self
        if _user_needs_window.is_set() or not _launch_lock.acquire(blocking=False):
            return self
        self.held = True
        if _user_needs_window.is_set():
            self.release()
        return self

    def release(self) -> None:
        if self.held:
            _launch_lock.release()
            self.held = False

    def __exit__(self, *_args: object) -> None:
        self.release()


def _singleton_pid(lock: Path) -> int | None:
    raw = ""
    try:
        raw = os.readlink(lock)
    except OSError:
        try:
            raw = lock.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    tail = str(raw).strip().rsplit("-", 1)[-1]
    try:
        return int(tail)
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _is_hidden_browser(pid: int) -> bool:
    """True for a headless tool browser. That process has no Mac window."""
    try:
        cmd = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "headless" in cmd.lower() and str(PROFILE_DIR) in cmd


def _stop_hidden_profile_browsers() -> None:
    """Stop headless browsers on this profile so a visible sign-in window can open."""
    needle = str(PROFILE_DIR)
    try:
        out = subprocess.check_output(["ps", "-ax", "-o", "pid=,command="], text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return
    pids: list[int] = []
    for line in out.splitlines():
        pid_s, _, cmd = line.strip().partition(" ")
        if needle not in cmd or "headless" not in cmd.lower():
            continue
        try:
            pids.append(int(pid_s))
        except ValueError:
            continue
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    for _ in range(20):
        if not any(_pid_alive(pid) for pid in pids):
            return
        time.sleep(0.1)


def _raise_tool_window() -> None:
    """Put the sign-in window in front. System Events is blocked on this Mac."""
    app = _tool_chrome_app()
    if app is None:
        return
    try:
        subprocess.run(["open", "-a", str(app)], timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def _stop_tool_chrome(pid: int) -> None:
    """Stop a leftover tool browser. Never the person's normal Chrome."""
    try:
        cmd = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if str(PROFILE_DIR) not in cmd and str(BROWSERS_DIR) not in cmd:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(20):
        if not _pid_alive(pid):
            return
        time.sleep(0.1)


def _release_profile_lock(*, headed: bool) -> str | None:
    """Clear a lock left by a dead, hidden, or leftover tool browser."""
    lock = PROFILE_DIR / "SingletonLock"
    pid = _singleton_pid(lock) if lock.exists() or lock.is_symlink() else None
    if pid and _pid_alive(pid) and not _is_hidden_browser(pid):
        if not headed:
            # A background refresh must not close the window someone is using.
            return (
                "A sign-in window is already open. Use that window to finish Duo, "
                "then leave it up until CAMGR loads."
            )
        # Duo's Continue button dismisses the window and leaves this process
        # holding the profile, so the next Log in says it is already open
        # even though nothing is on screen. Replace that leftover.
        _stop_tool_chrome(pid)
        if _pid_alive(pid):
            _raise_tool_window()
            return (
                "A sign-in window is already open. It should be in front now. "
                "Finish Duo there and leave it up until CAMGR loads."
            )
    _stop_hidden_profile_browsers()
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            (PROFILE_DIR / name).unlink(missing_ok=True)
        except OSError:
            pass
    return None


# Duo's "open in browser" button calls window.close() and also opens a tab in
# the main browser, behind the tool page. Block the close, and keep a second
# tab so the Chromium window itself cannot disappear.
_HOLD_SCRIPT = """
(() => {
  const block = function () {};
  try {
    Object.defineProperty(window, "close", { configurable: true, writable: false, value: block });
  } catch (e) {
    window.close = function () {};
  }
})();
"""
_KEEPER_TITLE = "Leave this window open"
_KEEPER_HTML = """<!DOCTYPE html><html><head><title>Leave this window open</title></head>
<body style="font-family:-apple-system,sans-serif;padding:2.5rem;line-height:1.45">
<h1>Leave this window open</h1>
<p>Finish the Duo prompt. If a tab opened in your main browser, it is being brought forward.</p>
<p>This window stays here and then signs in to CAMGR. You can ignore it until that page loads.</p>
</body></html>"""
_last_duo_focus = 0.0


def _hold_window_open(context: Any) -> None:
    """Stop Duo from closing the tool window when it opens the main browser."""
    try:
        context.add_init_script(_HOLD_SCRIPT)
    except Exception:
        return
    for page in _safe_pages(context):
        try:
            page.evaluate(_HOLD_SCRIPT)
        except Exception:
            continue


def _is_keeper(page: Any) -> bool:
    try:
        return _KEEPER_TITLE in (page.title() or "")
    except Exception:
        return False


def _login_pages(context: Any) -> list[Any]:
    return [page for page in _safe_pages(context) if not _is_keeper(page)]


def _page_host(page: Any) -> str:
    try:
        return (urlparse(page.url or "").hostname or "").lower()
    except Exception:
        return ""


def _dcloud_page(context: Any) -> Any | None:
    """The tab that already reached dCloud, not a second Duo prompt."""
    for page in _login_pages(context):
        if "dcloud2-" in _page_host(page):
            return page
    return None


def _work_page(context: Any) -> Any | None:
    """A tab CAMGR can load in. The extra Duo tab is left alone."""
    for page in _login_pages(context):
        if not _is_idp_page(page):
            return page
    return None


def _show_sign_in(context: Any) -> None:
    """The dCloud tab stays in front. The extra Duo tab is not the sign-in."""
    page = _dcloud_page(context)
    if page is None:
        for candidate in _login_pages(context):
            if _is_idp_page(candidate):
                page = candidate
                break
    if page is None:
        pages = _login_pages(context)
        page = pages[-1] if pages else None
    if page is None:
        return
    try:
        page.bring_to_front()
    except Exception:
        pass


def _open_keeper(context: Any) -> Any | None:
    """Keep the Cisco page in the one sign-in window. Do not open a second window."""
    _show_sign_in(context)
    return None


def _focus_duo_tab() -> None:
    """The Duo tab opens in the main browser behind the tool page. Bring it forward."""
    global _last_duo_focus
    now = time.time()
    if now - _last_duo_focus < 2.0:
        return
    _last_duo_focus = now
    script = r'''
tell application "System Events"
  set browserNames to {"Google Chrome", "Arc", "Microsoft Edge", "Brave Browser", "Chromium", "Safari"}
  set runningNames to name of every application process
end tell
repeat with appName in browserNames
  if runningNames contains appName then
    try
      tell application appName
        repeat with w in windows
          set i to 0
          repeat with t in tabs of w
            set i to i + 1
            try
              set tabURL to URL of t
            on error
              set tabURL to ""
            end try
            if tabURL contains "duosecurity" or tabURL contains "duo.com" then
              set active tab index of w to i
              set index of w to 1
              activate
              return
            end if
          end repeat
        end repeat
      end tell
    end try
  end if
end repeat
'''
    try:
        subprocess.run(["osascript", "-e", script], timeout=4, capture_output=True, check=False)
    except (OSError, subprocess.SubprocessError):
        pass




def _launch_failure(exc: Exception) -> str:
    text = str(exc)
    if text.startswith("A sign-in window"):
        return text
    if (
        "existing browser session" in text
        or "profile is already in use" in text
        or "kill EPERM" in text
    ):
        return (
            "A sign-in window is already open, or the last one is stuck. "
            "Quit that Chromium window, then click Log in again. If macOS says Terminal "
            "was blocked, allow it under System Settings → Privacy & Security → Files & Folders."
        )
    first = text.split("Call log:", 1)[0].strip()
    if len(first) > 240:
        first = first[:240].rsplit(" ", 1)[0] + "…"
    return f"Could not open the sign-in browser. {first}"


def _launch(playwright: Any, *, headed: bool):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    stuck = _release_profile_lock(headed=headed)
    if stuck:
        raise RuntimeError(stuck)
    args = ["--disable-blink-features=AutomationControlled"]
    if not headed:
        # A background refresh must not pop a window the person then sees close.
        args.append("--headless=new")
    launch_args: dict[str, Any] = {
        "headless": not headed,
        "viewport": {"width": 1200, "height": 860},
        "ignore_default_args": ["--enable-automation"],
        "args": args,
    }
    executable = _sign_in_chrome_executable()
    if executable:
        launch_args["executable_path"] = executable
    context = playwright.chromium.launch_persistent_context(str(PROFILE_DIR), **launch_args)
    context.add_init_script(_chrome_brand_script())
    return context


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
    with _BrowserTurn(headed) as turn:
        if not turn.held:
            return None, "The sign-in browser is already open."
        with sync_playwright() as playwright:
            try:
                context = _launch(playwright, headed=headed)
            except Exception as exc:
                return None, _launch_failure(exc)
            if headed:
                _set_headed_open(True)
                _hold_window_open(context)
                _open_keeper(context)
                _raise_tool_window()
            try:
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=15_000 if not headed else 60_000,
                    )
                except Exception as exc:
                    if not headed:
                        return None, "A sign-in is needed in the tool browser."
                    # The Duo page is often already on screen when this wait
                    # gives up. Closing here takes the prompt away before the
                    # person can click Continue in browser.
                    if not _safe_pages(context):
                        _open_target_page(context, url)
                if headed:
                    _show_sign_in(context)
                last_header = ""
                started = time.time()
                saw_idp = False
                opened_target_after_sso = False
                reopened_blank = False
                while time.time() < deadline:
                    if not headed and _user_needs_window.is_set():
                        return None, "A sign-in window was requested."
                    # The keeper tab does not count. Duo can close the prompt
                    # tab; that used to look like "no pages" and context.close()
                    # ran before any CAMGR cookie existed.
                    pages = _login_pages(context) if headed else _safe_pages(context)
                    if headed and not pages:
                        _open_keeper(context)
                        # One replacement tab. Opening a new one every pass
                        # closes the window and brings it straight back.
                        if not reopened_blank:
                            reopened_blank = True
                            _raise_tool_window()
                            opened = _open_target_page(context, url)
                            if opened is not None:
                                page = opened
                                opened_target_after_sso = True
                                _hold_window_open(context)
                                try:
                                    page.bring_to_front()
                                except Exception:
                                    pass
                        time.sleep(0.4)
                        continue
                    if pages:
                        page = pages[-1]
                        if headed:
                            _show_sign_in(context)
                    if _still_on_idp(context) or _is_idp_page(page):
                        saw_idp = True
                        opened_target_after_sso = False
                        if headed:
                            _focus_duo_tab()
                        # Silent callers gain nothing by waiting out a login form.
                        if (
                            not headed
                            and time.time() - started > IDP_SETTLE_SECONDS
                        ):
                            return None, "A sign-in is needed in the tool browser."
                        time.sleep(1.2)
                        continue
                    if (
                        headed
                        and saw_idp
                        and not opened_target_after_sso
                        and not _page_on_hosts(page, hosts)
                    ):
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                        except Exception:
                            opened = _open_target_page(context, url)
                            if opened is not None:
                                page = opened
                        opened_target_after_sso = True
                        continue
                    # A cookie saved from an earlier visit is not proof that
                    # this window has reached the site. Closing on that cookie
                    # was taking the window down a few seconds after it opened.
                    if headed and not _page_on_hosts(page, hosts):
                        time.sleep(0.6)
                        continue
                    last_header = _cookie_header(context.cookies(), hosts)
                    try:
                        logged_in = bool(last_header and is_logged_in(last_header))
                    except Exception:
                        logged_in = False
                    if logged_in:
                        return last_header, "Signed in with the tool browser."
                    time.sleep(1.2)
                if last_header and is_logged_in(last_header):
                    return last_header, "Signed in with the tool browser."
                return None, (
                    "Timed out waiting for Cisco SSO in the tool browser. "
                    "Finish Duo if a prompt is showing, then click Connect again."
                )
            finally:
                _set_headed_open(False)
                try:
                    context.close()
                except Exception:
                    pass


def capture_dcloud_tokens(
    site: str = "rtp",
    *,
    headed: bool = True,
    timeout_s: float = 180,
    warm_hub: bool = True,
) -> tuple[str, str, str, str]:
    """Return (access, refresh, site, message) from the tool Chromium dCloud session.

    warm_hub: after SSO, also visit CAI/CAMGR so Content Manager Connect buttons
    are already signed in. Demo Usage passes False so dCloud login does not
    open a second CAMGR Duo prompt.
    """
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
    with _BrowserTurn(headed) as turn:
        if not turn.held:
            return "", "", site_code, "The sign-in browser is already open."
        with sync_playwright() as playwright:
            try:
                context = _launch(playwright, headed=headed)
            except Exception as exc:
                return "", "", site_code, _launch_failure(exc)
            if headed:
                _set_headed_open(True)
                _hold_window_open(context)
                _open_keeper(context)
                _raise_tool_window()
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
                try:
                    page.goto(
                        login_url,
                        wait_until="domcontentloaded",
                        timeout=15_000 if not headed else 60_000,
                    )
                except Exception as exc:
                    if not headed:
                        return "", "", site_code, DCLOUD_SIGN_IN_NEEDED
                    if not _safe_pages(context):
                        _open_target_page(context, login_url)
                if headed:
                    _show_sign_in(context)
                last_err = "Waiting for dCloud sign-in in the tool browser."
                started = time.time()

                def finish(access: str, refresh: str, found_site: str) -> tuple[str, str, str, str]:
                    # Content Manager wants CAI/CAMGR cookies in this same window.
                    # Demo Usage passes warm_hub=False so dCloud SSO is one login.
                    if headed:
                        if warm_hub:
                            try:
                                _store_hub_cookies(_warm_hub_sessions(page, context, deadline=deadline))
                            except Exception:
                                pass
                    return access, refresh, found_site, "Signed in with the tool browser."

                saw_idp = False
                code_landed_at = 0.0
                tried_codes: set[str] = set()
                reopened_login = False
                blank_since = 0.0
                while time.time() < deadline:
                    if not headed and _user_needs_window.is_set():
                        return "", "", site_code, "A sign-in window was requested."
                    pages = _login_pages(context) if headed else _safe_pages(context)
                    if headed and not pages:
                        # Continue in browser closes the tab, then Duo opens the
                        # return page itself. Opening CAMGR or a new login here
                        # is a second Duo prompt.
                        if blank_since <= 0:
                            blank_since = time.time()
                        _open_keeper(context)
                        for code in list(seen):
                            if not code or code in tried_codes:
                                continue
                            tried_codes.add(code)
                            access, refresh, _expires, err = exchange_dcloud_access_code(
                                site_code, code
                            )
                            if access:
                                return finish(access, refresh, site_code)
                            last_err = err or last_err
                        if time.time() - blank_since < 6 or seen or reopened_login:
                            time.sleep(0.4)
                            continue
                        reopened_login = True
                        opened = _open_target_page(context, login_url)
                        if opened is not None:
                            page = opened
                            attach(page)
                            _hold_window_open(context)
                            try:
                                page.bring_to_front()
                            except Exception:
                                pass
                        time.sleep(0.4)
                        continue
                    blank_since = 0.0
                    if pages:
                        page = pages[-1]
                        if headed:
                            _show_sign_in(context)
                    # Silent callers must not sit here: once the redirects settle on an
                    # identity-provider page, only a real person can move it forward.
                    if (
                        not headed
                        and time.time() - started > IDP_SETTLE_SECONDS
                        and _is_idp_page(page)
                    ):
                        return "", "", site_code, DCLOUD_SIGN_IN_NEEDED
                    # The authenticate page is already spending this code.
                    # The extra Duo tab is not another sign-in. Leave it alone
                    # and finish from the dCloud tab. Clicking that Duo tab is
                    # what closes the window.
                    dcloud = _dcloud_page(context) if headed else None
                    if headed and dcloud is not None:
                        _remember_page_code(dcloud, seen)
                        on_authenticate = "authenticate" in _page_path(dcloud)
                        if code_landed_at <= 0 and (seen or on_authenticate):
                            code_landed_at = time.time()
                        access, refresh, found_site = _read_dcloud_storage(dcloud, site_code)
                        pending_code = _oauth_code_from_pages(context) or (seen[-1] if seen else "")
                        # An expired dc_p_a is the previous login. Accepting it
                        # shows a token and then the next status check wipes it.
                        # On the authenticate page, wait for the new login to
                        # replace that saved token before trusting it.
                        if _access_is_live(access) and not (pending_code or on_authenticate):
                            return finish(access, refresh, found_site)
                        if (
                            pending_code
                            and pending_code not in tried_codes
                            and code_landed_at > 0
                            and time.time() - code_landed_at >= 4
                        ):
                            if _access_is_live(access):
                                return finish(access, refresh, found_site)
                            tried_codes.add(pending_code)
                            access, refresh, _expires, err = exchange_dcloud_access_code(
                                site_code, pending_code
                            )
                            if access:
                                return finish(access, refresh, site_code)
                            last_err = err or last_err
                        if seen or on_authenticate:
                            time.sleep(0.5)
                            continue
                    if headed and _still_on_idp(context):
                        saw_idp = True
                        _focus_duo_tab()
                        time.sleep(0.5)
                        continue
                    if headed and time.time() - started < SSO_SETTLE_SECONDS:
                        time.sleep(0.5)
                        continue
                    for code in (*seen, _oauth_code_from_pages(context)):
                        if not code or code in tried_codes:
                            continue
                        tried_codes.add(code)
                        access, refresh, _expires, err = exchange_dcloud_access_code(site_code, code)
                        if access:
                            return finish(access, refresh, site_code)
                        last_err = err or last_err
                    seen.clear()
                    # The app may also have exchanged the code itself by now.
                    storage_page = _dcloud_page(context) or page
                    access, refresh, found_site = _read_dcloud_storage(storage_page, site_code)
                    if _access_is_live(access):
                        return finish(access, refresh, found_site)
                    time.sleep(0.6)
                return "", "", site_code, last_err
            finally:
                _set_headed_open(False)
                try:
                    context.close()
                except Exception:
                    pass


def _page_path(page: Any) -> str:
    try:
        return urlparse(page.url or "").path or ""
    except Exception:
        return ""


def _remember_page_code(page: Any, seen: list[str]) -> None:
    """The address bar drops the code while 'Logging you in' is still spinning."""
    code = ""
    try:
        code = _code_from_url(page.url or "")
    except Exception:
        code = ""
    if not code:
        try:
            original = page.evaluate(
                """() => {
                  const nav = performance.getEntriesByType('navigation')[0];
                  return (nav && nav.name) || '';
                }"""
            )
        except Exception:
            original = ""
        code = _code_from_url(str(original or ""))
    if code and code not in seen:
        seen.append(code)


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


def _access_is_live(access: str) -> bool:
    """A stored dc_p_a from an earlier login is not a new sign-in once it has expired."""
    text = (access or "").strip()
    if not text:
        return False
    exp = jwt_expires_at(text)
    if not exp:
        return True
    return time.time() < exp - 30


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
