"""Local tool: schedule dCloud sessions and manage saved content (CAI/CAMGR transfer, integrate, replace, cleanup)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse

APP_DIR = Path(__file__).resolve().parent
_SCRIPTING_ROOT = APP_DIR.parent


def _read_app_version() -> str:
    path = APP_DIR / "VERSION"
    try:
        line = path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return "0.0"
    return line or "0.0"


# Prefer a bundled browser_auth/ in this folder so a coworker zip is self-contained.
# Fall back to the sibling scripting/browser_auth used on the original machine.
for _root in (_SCRIPTING_ROOT, APP_DIR):
    _root_s = str(_root)
    if _root_s in sys.path:
        sys.path.remove(_root_s)
    sys.path.insert(0, _root_s)

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from browser_auth.browser_dcloud_auth import (
    clear_login_scan_state,
    mark_login_exchange_started,
    scan_dcloud_refresh_from_chrome,
    try_import_dcloud_session,
    try_import_dcloud_token,
)
from browser_auth.dcloud_oauth import (
    build_dcloud_login_url,
    dcloud_auth_status,
    fetch_dcloud_access_token,
    refresh_dcloud_user_token,
    resolve_dcloud_token,
)
from browser_auth.dcloud_token import (
    DCLOUD_SITES,
    effective_dcloud_token,
    jwt_expires_at,
    normalize_dcloud_token,
    validate_dcloud_token,
)
from dcloud_client import (
    admin_records_cached_at,
    attach_vm_access_links,
    apply_tbv3_power_states,
    catalog_search,
    check_public_session_status,
    delete_saved_content,
    decline_surveys,
    edit_topology_url,
    end_session,
    extend_session,
    parse_schedule_datetime,
    resolve_schedule_window,
    _dcloud_timestamp,
    fetch_content,
    fetch_admin_records,
    fetch_admin_content_panels,
    fetch_session_log,
    update_session_schedule,
    extract_content_topology_uid,
    extract_parent_content_id,
    fetch_content_shared_with,
    fetch_session,
    fetch_session_shared_with,
    format_status,
    guest_shutdown_vms,
    is_active_status,
    is_auth_error,
    is_failed_status,
    is_saved_status,
    is_saving_in_progress_status,
    is_stopping_status,
    list_dashboard_sessions_all_sites,
    resolve_monitor_sessions,
    list_pending_surveys_all_sites,
    list_saved_contents_all_sites,
    list_content_vms,
    list_session_vms,
    lookup_demo_ids_across_sites,
    match_selected_vms,
    parse_site_and_id,
    power_on_vms,
    reset_session,
    save_session,
    schedule_exported_session,
    find_schedule_conflict,
    search_share_users,
    search_admin_records,
    session_info_panels,
    session_owner,
    session_saved_content_id,
    session_view_url,
    shared_with_from_details,
    owner_is_me,
    token_identities,
    site_base,
    tbv3_edit_url,
    TBV3_UI,
    update_content_share,
    update_session_name,
    update_session_share,
    POWER_ON_STATES,
    SITES,
    tag_selected_vms,
    vm_action,
    wait_for_power_state,
    wait_until_active,
)

from cai_client import (
    CAI_HOME,
    cai_dc_label,
    cai_dc_to_dcloud_site,
    cai_demo_url,
    cai_integrate_dests,
    cai_integrate_dc_chips,
    cai_replace_vm_chips,
    fetch_demo_page,
    import_cai_cookies_from_chrome,
    list_cai_tasks,
    match_integrate_task,
    match_replace_task,
    match_template_tasks,
    cookie_header_is_tracking_only,
    normalize_cai_dc,
    normalize_cookie_header,
    normalize_task_status,
    parse_cai_task_dc,
    probe_cai_login,
    submit_integrate,
    submit_template,
    submit_vm_replace,
)
from camgr_client import (
    CAMGR_HOME,
    CAMGR_LOGIN_HINT,
    IN_FLIGHT_STATUSES,
    camgr_guid_to_site,
    fetch_camgr_demo,
    fetch_camgr_vms,
    format_camgr_status,
    camgr_dc_status_chips,
    camgr_cdev_home_dc,
    fetch_camgr_vpod_vms,
    import_camgr_cookies_from_chrome,
    is_cdev_camgr_guid,
    list_camgr_jobs,
    list_camgr_servers,
    probe_camgr_login,
    public_camgr_job,
    refresh_camgr_job,
    set_camgr_cookie_sink,
    site_to_camgr_guid,
    submit_camgr_transfer,
    submit_camgr_vpod_transfer,
)

from camgr_tab import connect_camgr_via_chrome_tab
from camgr_browser import capture_camgr_session
from net_errors import host_resolves, off_network_message

load_dotenv()

# Internal-only hosts: neither resolves off the Cisco VPN / office network.
CAI_HOST = urlparse(CAI_HOME).hostname or "dcloud-cai.cisco.com"
CAMGR_HOST = urlparse(CAMGR_HOME).hostname or "dcloud-camgr.cisco.com"

STATIC_DIR = APP_DIR / "static"
ENV_FILE = APP_DIR / ".env"
ENV_EXAMPLE_FILE = APP_DIR / ".env.example"
APP_VERSION = _read_app_version()
LAST_JOB_FILE = APP_DIR / "last-job.json"
SESSION_FILE = APP_DIR / ".dcloud-session.json"
CAI_SESSION_FILE = APP_DIR / ".dcloud-cai-session.json"
CAMGR_SESSION_FILE = APP_DIR / ".dcloud-camgr-session.json"
MANAGED_SAVED_IDS_FILE = APP_DIR / "managed-saved-ids.json"
_AUTO_RESTORE_HOURS = float(os.getenv("DCLOUD_AUTO_RESTORE_HOURS", "4"))
_AUTO_RESTORE_MAX_AGE_SECS = max(3600.0, _AUTO_RESTORE_HOURS * 3600.0)
_TERMINAL_DC_PHASES = frozenset({"saved", "ended"})
# A card left over from an earlier run is never swept into a bulk save. Saving one
# still works from its own card, where you picked that session on purpose.
_BULK_SAVE_MAX_AGE_SECS = max(3600.0, float(os.getenv("DCLOUD_BULK_SAVE_HOURS", "8")) * 3600.0)
# Finished cards older than this are dropped on startup instead of piling up.
_FINISHED_CARD_KEEP_SECS = max(3600.0, float(os.getenv("DCLOUD_KEEP_FINISHED_HOURS", "12")) * 3600.0)
_SKIP_REFRESH_DC_PHASES = _TERMINAL_DC_PHASES | frozenset({"ending", "saving", "shutting_down"})
# Phases the background watcher keeps polling until the session settles.
_WATCHED_DC_PHASES = frozenset(
    {"waiting", "queued", "scheduling", "resetting", "saving", "shutting_down"}
)
# A reset tears the session down and rebuilds it, so it goes missing for a while.
RESET_GRACE_SECONDS = 30 * 60

app = FastAPI(title="dCloud Content Manager", version=APP_VERSION)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, HTTPException):
        raise exc
    _log_trace = traceback.format_exc()
    print(_log_trace, flush=True)
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc) or "Unexpected server error."},
    )

_jobs_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}

_user_auth_lock = threading.Lock()
_user_auth: dict[str, Any] = {
    "access_token": "",
    "refresh_token": "",
    "site": "rtp",
    "expires_at": 0.0,
    "source": "",
    "has_refresh": False,
}

_cai_auth_lock = threading.Lock()
_cai_auth: dict[str, Any] = {
    "cookie": "",
    "imported_at": 0.0,
    "message": "",
    "network_ok": False,
}

_camgr_auth_lock = threading.Lock()
_camgr_auth: dict[str, Any] = {
    "cookie": "",
    "imported_at": 0.0,
    "message": "",
    "user": "",
    # True once a probe succeeded, so status calls do not have to re-probe CAMGR.
    "verified": False,
    "verified_at": 0.0,
}
# A verified session is trusted this long before an action re-checks it.
CAMGR_VERIFY_TTL_SECONDS = 90
CAMGR_BROWSER_PROFILE = APP_DIR / ".dcloud-camgr-chrome"
_camgr_open_lock = threading.Lock()
_camgr_open: dict[str, Any] = {"running": False, "error": ""}

_auth_keepalive_lock = threading.Lock()
_auth_keepalive_started = False
# While signed in, touch the services now and then to keep the sessions warm.
AUTH_KEEPALIVE_SECONDS = 240
# While signed out, look for a fresh Chrome login often so nobody has to click Connect.
AUTH_WATCH_SECONDS = 15
# Reading every Chrome profile is slow, so back off between failed auto-imports.
CHROME_IMPORT_MIN_SECONDS = 4
_chrome_import_lock = threading.Lock()
_chrome_import_last: dict[str, float] = {"camgr": 0.0, "cai": 0.0}


def _chrome_import_allowed(kind: str) -> bool:
    now = time.time()
    with _chrome_import_lock:
        if now - _chrome_import_last.get(kind, 0.0) < CHROME_IMPORT_MIN_SECONDS:
            return False
        _chrome_import_last[kind] = now
    return True


def _chrome_import_reset(kind: str) -> None:
    with _chrome_import_lock:
        _chrome_import_last[kind] = 0.0

_managed_saved_lock = threading.Lock()
_auto_integrate_lock = threading.Lock()
_auto_integrate_busy: set[str] = set()
_AUTO_INTEGRATE_MAX_ATTEMPTS = 36
_burn_in_lock = threading.Lock()
_burn_in_busy: set[str] = set()


def _load_cai_session() -> None:
    if not CAI_SESSION_FILE.is_file():
        return
    try:
        data = json.loads(CAI_SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return
    cookie = str(data.get("cookie") or "").strip()
    if not cookie:
        return
    with _cai_auth_lock:
        _cai_auth["cookie"] = cookie
        _cai_auth["imported_at"] = float(data.get("imported_at") or 0)


def _persist_cai_session() -> None:
    with _cai_auth_lock:
        cookie = str(_cai_auth.get("cookie") or "").strip()
        imported_at = float(_cai_auth.get("imported_at") or 0)
    if not cookie:
        try:
            CAI_SESSION_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return
    try:
        CAI_SESSION_FILE.write_text(
            json.dumps({"cookie": cookie, "imported_at": imported_at}, indent=2) + "\n",
            encoding="utf-8",
        )
        CAI_SESSION_FILE.chmod(0o600)
    except OSError:
        pass


def _cai_cookie() -> str:
    with _cai_auth_lock:
        return str(_cai_auth.get("cookie") or "").strip()


def _set_cai_cookie(cookie: str, message: str = "") -> None:
    with _cai_auth_lock:
        _cai_auth["cookie"] = (cookie or "").strip()
        _cai_auth["imported_at"] = time.time() if cookie else 0.0
        _cai_auth["message"] = message
        if not cookie:
            _cai_auth["network_ok"] = False
    _persist_cai_session()


def _mark_cai_reachable(message: str, cookie: str = "") -> None:
    with _cai_auth_lock:
        _cai_auth["cookie"] = (cookie or "").strip()
        _cai_auth["imported_at"] = time.time() if cookie else 0.0
        _cai_auth["message"] = message
        _cai_auth["network_ok"] = True
    _persist_cai_session()


def _cai_public_status(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    cookie = _cai_cookie()
    with _cai_auth_lock:
        network_ok = bool(_cai_auth.get("network_ok"))
        stored_message = str(_cai_auth.get("message") or "")
    stored_message = stored_message.replace(", no login cookie needed.", ".").replace(" No login cookie needed.", "")
    payload = {
        "loggedIn": network_ok,
        "configured": bool(cookie) or network_ok,
        "homeUrl": CAI_HOME,
        "message": stored_message
        or "On the Cisco network, click Connect to CAI.",
    }
    if extra:
        payload.update(extra)
    elif cookie and not extra:
        payload["message"] = stored_message or "CAI session saved. Load VMs to confirm it is still valid."
    payload["configured"] = bool(cookie) or bool(payload.get("loggedIn"))
    return payload


def _load_camgr_session() -> None:
    if not CAMGR_SESSION_FILE.is_file():
        return
    try:
        data = json.loads(CAMGR_SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return
    cookie = str(data.get("cookie") or "").strip()
    if not cookie:
        return
    with _camgr_auth_lock:
        _camgr_auth["cookie"] = cookie
        _camgr_auth["imported_at"] = float(data.get("imported_at") or 0)
        _camgr_auth["user"] = str(data.get("user") or "").strip()


def _persist_camgr_session() -> None:
    with _camgr_auth_lock:
        cookie = str(_camgr_auth.get("cookie") or "").strip()
        imported_at = float(_camgr_auth.get("imported_at") or 0)
        user = str(_camgr_auth.get("user") or "").strip()
    if not cookie:
        try:
            CAMGR_SESSION_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return
    try:
        CAMGR_SESSION_FILE.write_text(
            json.dumps(
                {"cookie": cookie, "imported_at": imported_at, "user": user},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        CAMGR_SESSION_FILE.chmod(0o600)
    except OSError:
        pass


def _camgr_cookie() -> str:
    with _camgr_auth_lock:
        return str(_camgr_auth.get("cookie") or "").strip()


def _camgr_current_user() -> str:
    with _camgr_auth_lock:
        return str(_camgr_auth.get("user") or "").strip()


def _camgr_owner_is_mine(owner: str, *, allow_empty: bool = False) -> bool:
    me = _camgr_current_user().strip().lower()
    them = str(owner or "").strip().lower()
    if me and them:
        return them == me
    return allow_empty or not them


def _camgr_mark_unverified(message: str = "") -> None:
    """A call failed, but keep the cookie so a retry or Chrome re-import can repair it."""
    with _camgr_auth_lock:
        _camgr_auth["verified"] = False
        if message:
            _camgr_auth["message"] = message


def _camgr_cookie_rotated(cookie: str) -> None:
    """CAMGR handed back a newer session cookie; keep it so it does not go stale."""
    header = (cookie or "").strip()
    if not header:
        return
    with _camgr_auth_lock:
        current = str(_camgr_auth.get("cookie") or "").strip()
        if not current or current == header:
            return
        _camgr_auth["cookie"] = header
        _camgr_auth["imported_at"] = time.time()
    _persist_camgr_session()


def _set_camgr_cookie(cookie: str, message: str = "", user: str = "") -> None:
    with _camgr_auth_lock:
        _camgr_auth["cookie"] = (cookie or "").strip()
        _camgr_auth["imported_at"] = time.time() if cookie else 0.0
        _camgr_auth["message"] = message
        _camgr_auth["user"] = user if cookie else ""
        _camgr_auth["verified"] = bool(cookie)
        _camgr_auth["verified_at"] = time.time() if cookie else 0.0
    _persist_camgr_session()


def _camgr_public_status(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    cookie = _camgr_cookie()
    with _camgr_auth_lock:
        stored_message = str(_camgr_auth.get("message") or "")
        user = str(_camgr_auth.get("user") or "")
        verified = bool(_camgr_auth.get("verified"))
    payload = {
        "loggedIn": bool(cookie) and verified,
        "configured": bool(cookie),
        "homeUrl": CAMGR_HOME,
        "user": user,
        "message": stored_message
        or CAMGR_LOGIN_HINT,
    }
    if extra:
        payload.update(extra)
    elif cookie:
        payload["message"] = stored_message or (
            "CAMGR session saved locally — Connect to confirm it is still valid."
        )
    payload["configured"] = bool(cookie) or bool(payload.get("loggedIn"))
    payload["user"] = str(payload.get("user") or user)
    with _camgr_open_lock:
        payload["openRunning"] = bool(_camgr_open.get("running"))
        open_error = str(_camgr_open.get("error") or "")
    if payload["openRunning"] and not payload.get("loggedIn"):
        payload["message"] = "Sign in in the CAMGR window if asked. This tool will connect when it is ready."
    elif open_error and not payload.get("loggedIn"):
        payload["openError"] = open_error
        payload["message"] = open_error
    return payload


class TokenPayload(BaseModel):
    dcloud_token: str | None = None
    dcloud_token_source: str = "browser"


def _env_auth_allowed() -> bool:
    """When false (default), only per-user browser import or pasted token is accepted."""
    return os.getenv("DCLOUD_ALLOW_ENV_AUTH", "").strip().lower() in ("1", "true", "yes")


def _apply_user_session(
    access: str,
    refresh: str | None,
    site: str | None,
    source: str,
) -> None:
    with _user_auth_lock:
        _user_auth["access_token"] = (access or "").strip()
        if refresh:
            _user_auth["refresh_token"] = refresh
            _user_auth["has_refresh"] = True
        if site:
            _user_auth["site"] = site
        _user_auth["source"] = source
        if (access or "").strip():
            exp = jwt_expires_at(access)
            _user_auth["expires_at"] = exp or (time.time() + 3600)
    _persist_user_session()


def _persist_user_session() -> None:
    with _user_auth_lock:
        snap = {k: _user_auth.get(k) for k in ("access_token", "refresh_token", "site", "expires_at", "source")}
    if not (snap.get("refresh_token") or snap.get("access_token")):
        try:
            SESSION_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return
    try:
        SESSION_FILE.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    except OSError:
        pass


def _maybe_backfill_refresh_from_chrome() -> bool:
    """If access token is saved but refresh is missing, read dc_p_r from Chrome."""
    with _user_auth_lock:
        if (_user_auth.get("refresh_token") or "").strip():
            return False
        if not (_user_auth.get("access_token") or "").strip():
            return False
    refresh = scan_dcloud_refresh_from_chrome()
    if not refresh:
        return False
    with _user_auth_lock:
        _user_auth["refresh_token"] = refresh
        _user_auth["has_refresh"] = True
    _persist_user_session()
    return True


def _load_persisted_session() -> None:
    if not SESSION_FILE.is_file():
        return
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return
    access = (data.get("access_token") or "").strip()
    refresh = (data.get("refresh_token") or "").strip() or None
    if not access and not refresh:
        return
    site = (data.get("site") or "rtp").strip().lower()
    source = (data.get("source") or "browser").strip().lower()
    stored_exp = float(data.get("expires_at") or 0)
    if access:
        _apply_user_session(access, refresh, site, source)
    else:
        with _user_auth_lock:
            _user_auth.update(
                {
                    "access_token": "",
                    "refresh_token": refresh or "",
                    "has_refresh": bool(refresh),
                    "site": site,
                    "source": source,
                    "expires_at": stored_exp,
                }
            )
        _persist_user_session()
        return
    if stored_exp:
        with _user_auth_lock:
            _user_auth["expires_at"] = stored_exp
        _persist_user_session()
    _maybe_backfill_refresh_from_chrome()


def _ensure_user_access_token(
    progress: Callable[[str], None] | None = None,
) -> str:
    """Return a valid access token, refreshing from dc_p_r in memory when the JWT expired."""
    with _user_auth_lock:
        token = (_user_auth.get("access_token") or "").strip()
        expires_at = float(_user_auth.get("expires_at") or 0)
        refresh = (_user_auth.get("refresh_token") or "").strip()
        site = (_user_auth.get("site") or "rtp").strip().lower()
    if token and (not expires_at or time.time() < expires_at - 60):
        return token
    if refresh:
        sites: list[str] = []
        if site:
            sites.append(site)
        for code in DCLOUD_SITES:
            if code not in sites:
                sites.append(code)
        last_err = ""
        for try_site in sites:
            access, _new_refresh, _expires_at, err = _refresh_session_token(
                refresh, try_site, progress
            )
            if access:
                return access
            if err:
                last_err = err
        if progress and last_err:
            progress(last_err)
    return ""


def _cached_user_access_token() -> str:
    return _ensure_user_access_token()


def _copy_session_to_job(job: dict[str, Any]) -> None:
    with _user_auth_lock:
        refresh = (_user_auth.get("refresh_token") or "").strip()
        site = (_user_auth.get("site") or "rtp").strip().lower()
        expires_at = float(_user_auth.get("expires_at") or 0)
    if refresh:
        job["refresh_token"] = refresh
        job["site"] = site
    if expires_at:
        job["token_expires_at"] = expires_at
    elif job.get("token"):
        exp = jwt_expires_at(str(job.get("token") or ""))
        if exp:
            job["token_expires_at"] = exp


def _refresh_session_token(
    refresh_token: str,
    site: str,
    progress: Callable[[str], None] | None = None,
) -> tuple[str, str, float, str | None]:
    access, new_refresh, expires_in, err = refresh_dcloud_user_token(site, refresh_token)
    if err or not access:
        return "", "", 0.0, err or "Refresh failed."
    expires_at = jwt_expires_at(access) or (time.time() + max(expires_in, 300))
    _apply_user_session(access, new_refresh or refresh_token, site, "browser")
    if progress:
        progress("Refreshed dCloud token automatically.")
    return access, new_refresh or refresh_token, expires_at, None


def _refresh_job_token(
    job: dict[str, Any],
    progress: Callable[[str], None] | None = None,
) -> tuple[str, str | None]:
    refresh = (job.get("refresh_token") or "").strip()
    site = (job.get("site") or "rtp").strip().lower()
    if not refresh:
        with _user_auth_lock:
            refresh = (_user_auth.get("refresh_token") or "").strip()
            if _user_auth.get("site"):
                site = (_user_auth.get("site") or site).strip().lower()
    if not refresh:
        return "", "No refresh token — log in or import from Chrome again."
    access, new_refresh, expires_at, err = _refresh_session_token(refresh, site, progress)
    if err or not access:
        return "", err
    job["token"] = access
    job["token_at"] = time.time()
    job["token_expires_at"] = expires_at
    job["refresh_token"] = new_refresh
    job["site"] = site
    return access, None


class LoadVmsPayload(TokenPayload):
    site: str = ""
    session_id: str = ""
    content_id: str = ""
    source: str = "session"
    skip_catalog_lookup: bool = False


class DemoIds(BaseModel):
    sjc: str = ""
    rtp: str = ""
    lon: str = ""
    sng: str = ""
    syd: str = ""


class SelectedVm(BaseModel):
    name: str = ""
    displayName: str = ""
    shortName: str = ""
    mor: str = ""
    uid: str = ""


class ScheduleSiteDecision(BaseModel):
    site: str
    action: str  # schedule_next | skip
    start_at: str = ""
    stop_at: str = ""


class RunPayload(TokenPayload):
    demo_ids: DemoIds
    selected_vms: list[SelectedVm] = Field(default_factory=list)
    days: int = Field(default=1, ge=1)
    start_at: str = ""
    stop_at: str = ""
    active_timeout_minutes: int = Field(default=90, ge=10, le=240)
    content_export: bool = True
    auto_next_available: bool = True
    schedule_decisions: list[ScheduleSiteDecision] = Field(default_factory=list)
    job_id: str = ""
    # Set when the user confirmed scheduling with no VMs checked.
    skip_power_on: bool = False


class CaiDemoRef(BaseModel):
    site: str
    saved_id: str
    published_id: str = ""
    name: str = ""
    parent_id: str = ""


class CaiVmsPayload(BaseModel):
    site: str = ""
    saved_id: str = ""
    items: list[CaiDemoRef] = Field(default_factory=list)


class CaiReplaceItem(BaseModel):
    site: str
    saved_id: str
    target_id: str = ""
    vms: list[str] = Field(default_factory=list)


class CaiReplacePayload(BaseModel):
    job_id: str = ""
    vms: list[str] = Field(default_factory=list)
    items: list[CaiReplaceItem] = Field(default_factory=list)


class CaiRefreshPayload(BaseModel):
    job_id: str = ""


class CaiIntegratePayload(BaseModel):
    job_id: str = ""
    dcs: list[str] = Field(default_factory=list)
    items: list[CaiDemoRef] = Field(default_factory=list)


class CaiTemplatePayload(BaseModel):
    source_path: str = ""
    server: str = ""
    dcs: list[str] = Field(default_factory=list)


class CaiHidePayload(BaseModel):
    job_id: str = ""
    items: list[CaiDemoRef] = Field(default_factory=list)


class SavedIdsAddPayload(BaseModel):
    job_id: str = ""
    items: list[CaiDemoRef] = Field(default_factory=list)


class SavedIdsLookupPayload(BaseModel):
    job_id: str = ""
    site: str
    saved_id: str


class CaiCookiePayload(BaseModel):
    cookie: str = ""


class CamgrCookiePayload(BaseModel):
    cookie: str = ""


class CamgrTransferPayload(BaseModel):
    job_id: str = ""
    vm_names: list[str] = Field(default_factory=list)
    dcs: list[str] = Field(default_factory=list)
    integrate: bool = False
    integrate_name: str = ""
    integrate_wait: bool = False
    auto_integrate: bool = False
    auto_burn_in: bool = False
    burn_in_days: int = Field(default=1, ge=1)
    items: list[CaiDemoRef] = Field(default_factory=list)


class CamgrVpodVmsPayload(BaseModel):
    guid: str = ""
    vpod: str = ""


class CamgrVpodTransferPayload(BaseModel):
    guid: str = ""
    vpod: str = ""
    vm_names: list[str] = Field(default_factory=list)
    dcs: list[str] = Field(default_factory=list)
    job_id: str = ""


class CamgrImportJobsPayload(BaseModel):
    job_id: str = ""
    items: list[CaiDemoRef] = Field(default_factory=list)


class SessionRef(BaseModel):
    site: str
    session_id: str = ""


class SessionSaveName(BaseModel):
    site: str = ""
    session_id: str = ""
    name: str = ""
    description: str = ""


class ShutdownPayload(TokenPayload):
    job_id: str = ""
    sites: list[str] = Field(default_factory=list)
    session_id: str = ""
    sessions: list[SessionRef] = Field(default_factory=list)
    save_url: str = ""
    save_method: str = "PUT"
    # True only for a single card's own button, which may target a monitoring card.
    single_card: bool = False
    save_name: str = ""
    # Per-session names so one bulk save can cover different demos.
    save_names: list[SessionSaveName] = Field(default_factory=list)
    save_description: str = ""


class EndPayload(TokenPayload):
    job_id: str = ""
    sites: list[str] = Field(default_factory=list)
    session_id: str = ""
    sessions: list[SessionRef] = Field(default_factory=list)
    # True only for a single card's own button, which may target a monitoring card.
    single_card: bool = False


class ResetPayload(TokenPayload):
    job_id: str = ""
    sites: list[str] = Field(default_factory=list)
    session_id: str = ""
    sessions: list[SessionRef] = Field(default_factory=list)


class LocalResetPayload(BaseModel):
    """Which local records Start fresh wipes. Nothing here touches dCloud."""

    job: bool = True
    monitoring: bool = True
    hub: bool = True
    log: bool = True


class ExtendPayload(TokenPayload):
    sessions: list[SessionRef] = Field(default_factory=list)
    stop_at: str = ""


class AttachSession(BaseModel):
    site: str
    session_id: str = ""


class AttachPayload(TokenPayload):
    sessions: list[AttachSession] = Field(default_factory=list)
    selected_vms: list[SelectedVm] = Field(default_factory=list)
    monitor_only: bool = False
    job_id: str = ""


class ResolveMonitorPayload(TokenPayload):
    site: str
    identifier: str


class MoveCardPayload(TokenPayload):
    site: str
    session_id: str = ""
    monitor_only: bool = False


class RemoveCardPayload(TokenPayload):
    site: str
    session_id: str = ""
    demo_id: str = ""


class RefreshStatusPayload(TokenPayload):
    site: str = ""
    session_id: str = ""


class ShareSearchPayload(TokenPayload):
    site: str
    query: str = ""
    kind: str = "session"


class ShareStatePayload(TokenPayload):
    site: str
    kind: str = "session"
    session_id: str = ""
    content_id: str = ""


class ShareUser(BaseModel):
    userId: str
    fullName: str = ""


class ShareUpdatePayload(TokenPayload):
    site: str
    kind: str = "session"
    session_id: str = ""
    content_id: str = ""
    shared_with: list[ShareUser] = Field(default_factory=list)
    job_id: str = ""


class RenameSessionPayload(TokenPayload):
    site: str
    session_id: str = ""
    name: str = Field(default="", max_length=255)


class DeleteContentItem(BaseModel):
    site: str
    content_id: str = ""


class DeleteContentsPayload(TokenPayload):
    job_id: str = ""
    items: list[DeleteContentItem] = Field(default_factory=list)


class SurveyRef(BaseModel):
    site: str
    survey_id: str = ""


class DeclineSurveysPayload(TokenPayload):
    items: list[SurveyRef] = Field(default_factory=list)
    decline_all: bool = False


class ScheduleSavedItem(BaseModel):
    site: str
    content_id: str = ""
    name: str = ""


class ScheduleSavedPayload(TokenPayload):
    items: list[ScheduleSavedItem] = Field(default_factory=list)
    selected_vms: list[SelectedVm] = Field(default_factory=list)
    days: int = Field(default=1, ge=1)
    start_at: str = ""
    stop_at: str = ""
    active_timeout_minutes: int = Field(default=90, ge=10, le=240)
    content_export: bool = False
    auto_next_available: bool = True
    schedule_decisions: list[ScheduleSiteDecision] = Field(default_factory=list)
    job_id: str = ""


class ScheduleConflictCheckPayload(TokenPayload):
    demo_ids: DemoIds = Field(default_factory=DemoIds)
    items: list[ScheduleSavedItem] = Field(default_factory=list)
    days: int = Field(default=1, ge=1)
    start_at: str = ""
    stop_at: str = ""


class UnifiedSearchPayload(TokenPayload):
    query: str = ""
    site: str = "rtp"
    sites: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    exact_catalog: bool = True
    refresh_data: bool = False


class SearchItemPayload(TokenPayload):
    site: str
    content_id: str = ""
    session_id: str = ""
    action: str = ""
    name: str = ""
    description: str = ""
    start_at: str = ""
    stop_at: str = ""


class CatalogIdsPayload(TokenPayload):
    name: str = ""


class VmActionPayload(TokenPayload):
    site: str
    session_id: str = ""
    name: str = ""
    mor: str = ""
    uid: str = ""
    action: str = "guestShutdown"


SESSION_EXPIRED_HINT = (
    "Your dCloud session expired. Click Log in to dCloud in Step 1 to sign in again."
)
NOT_SIGNED_IN_HINT = "Not signed in. Click Log in to dCloud in Step 1."


def _resolve_token(payload: TokenPayload, progress: Callable[[str], None] | None = None) -> str:
    source = (payload.dcloud_token_source or "browser").strip().lower()
    env_ok = _env_auth_allowed()

    if source == "oauth" and not env_ok:
        raise HTTPException(
            400,
            "Auto login from .env is disabled. Log in to dCloud or import your token from Chrome "
            "or paste it in Step 1 — sessions must run under your own login.",
        )

    if source == "paste":
        token = normalize_dcloud_token(payload.dcloud_token or "")
        if not token and env_ok:
            token = effective_dcloud_token(payload.dcloud_token) or ""
        if not token:
            raise HTTPException(
                400,
                "Paste your dCloud token in Step 1, or click Import from browser.",
            )
        return token

    if source in {"browser", "login"}:
        token = _cached_user_access_token()
        if token:
            return token
        with _user_auth_lock:
            has_refresh = bool((_user_auth.get("refresh_token") or "").strip())
        raise HTTPException(400, SESSION_EXPIRED_HINT if has_refresh else NOT_SIGNED_IN_HINT)

    token, err = resolve_dcloud_token(
        source,
        payload.dcloud_token,
        env_file=ENV_FILE,
        progress=progress,
    )
    if err or not token:
        raise HTTPException(400, err or "dCloud token is required.")
    return token


OAUTH_REFRESH_AFTER = 40 * 60
BROWSER_REFRESH_BEFORE = 5 * 60
AUTH_PAUSE_HINT = (
    "Token expired (401). Click Log in to dCloud or Import from browser in Step 1, "
    "then Continue."
)


def _job_token(job: dict[str, Any], progress: Callable[[str], None] | None = None) -> str:
    source = (job.get("token_source") or "").strip().lower()
    token = job.get("token") or ""
    if (
        _env_auth_allowed()
        and source == "oauth"
        and token
        and time.time() - float(job.get("token_at") or 0) > OAUTH_REFRESH_AFTER
    ):
        fresh, err = fetch_dcloud_access_token(ENV_FILE)
        if fresh:
            job["token"] = fresh
            job["token_at"] = time.time()
            if progress:
                progress("Refreshed dCloud OAuth token.")
            return fresh
        if progress and err:
            progress(err)
    expires_at = float(job.get("token_expires_at") or 0)
    if (
        source in {"browser", "login", "paste"}
        and (job.get("refresh_token") or _user_auth.get("refresh_token"))
        and expires_at
        and time.time() >= expires_at - BROWSER_REFRESH_BEFORE
    ):
        fresh, err = _refresh_job_token(job, progress)
        if fresh:
            return fresh
        if progress and err:
            progress(err)
    return token


def _recover_auth(
    job: dict[str, Any],
    progress: Callable[[str], None],
    stale_token: str,
) -> tuple[str, str | None]:
    """On 401: refresh OAuth automatically, otherwise pause for a Chrome re-import."""
    lock: threading.Lock = job.setdefault("auth_lock", threading.Lock())
    resume: threading.Event = job.setdefault("auth_resume", threading.Event())
    with lock:
        current = job.get("token") or ""
        if current and current != stale_token:
            return current, None
        source = (job.get("token_source") or "").strip().lower()
        if _env_auth_allowed() and source == "oauth":
            fresh, err = fetch_dcloud_access_token(ENV_FILE)
            if fresh:
                job["token"] = fresh
                job["token_at"] = time.time()
                progress("Refreshed dCloud OAuth token after 401.")
                return fresh, None
            progress(err or "OAuth refresh failed — pausing so you can import a browser token.")
        if source in {"browser", "login", "paste"} and (
            job.get("refresh_token") or _user_auth.get("refresh_token")
        ):
            fresh, err = _refresh_job_token(job, progress)
            if fresh:
                progress("Refreshed dCloud token after 401.")
                return fresh, None
            if err and progress:
                progress(err)
        if not job.get("auth_needed"):
            job["phase_before_pause"] = job.get("phase") or ""
            job["phase"] = "paused_auth"
            job["auth_needed"] = True
            job["auth_message"] = AUTH_PAUSE_HINT
            resume.clear()
            progress(AUTH_PAUSE_HINT)
    while not job["stop"].is_set():
        if resume.wait(timeout=1.0):
            token = job.get("token") or ""
            if token:
                return token, None
            return "", "No token after Continue. Import from Chrome, then Continue again."
    return "", "Stopped by user."


def _job(job_id: str) -> dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            return job
    _load_last_job()
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found.")
        return job


def _repair_false_saved_dcs(job: dict[str, Any]) -> None:
    """Fix cards wrongly marked saved when a session was ended (pre-fix jobs)."""
    changed = False
    for dc in job.get("dcs") or []:
        if str(dc.get("phase") or "") != "saved" or dc.get("saveConfirmed"):
            continue
        dc["phase"] = "ended"
        dc["endedWithoutSave"] = True
        dc["savedId"] = ""
        dc["savedName"] = ""
        dc["savedParentId"] = ""
        dc["contentViewUrl"] = ""
        if "saved" in str(dc.get("message") or "").lower():
            dc["message"] = "Session ended (not saved)."
        changed = True
    if changed:
        _sync_job_phase(job)


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    _backfill_session_names(job)
    _repair_false_saved_dcs(job)
    _annotate_dc_ownership(job)
    _persist_job(job)
    content_name = ""
    for dc in job.get("dcs") or []:
        content_name = str(dc.get("name") or "").strip()
        if content_name:
            break
    return {
        "id": job["id"],
        "phase": job["phase"],
        "dcs": job["dcs"],
        "log": job["log"],
        "error": job.get("error") or "",
        "createdAt": job["createdAt"],
        "updatedAt": job.get("updatedAt") or "",
        "contentExport": bool(job.get("contentExport", True)),
        "contentName": content_name,
        "savedIds": _saved_id_summary(job),
        "caiReplaces": list(job.get("caiReplaces") or []),
        "caiIntegrates": list(job.get("caiIntegrates") or []),
        "camgrTransfers": list(job.get("camgrTransfers") or []),
        "authNeeded": bool(job.get("auth_needed")),
        "authMessage": job.get("auth_message") or "",
        "tokenSource": job.get("token_source") or "",
        "selectedVms": job.get("selected_vms") or [],
    }


def _backfill_session_names(job: dict[str, Any]) -> None:
    """Fill session/content names on DC cards so the Save as box can show the real title."""
    if job.get("names_backfilled"):
        return
    missing = [
        dc
        for dc in (job.get("dcs") or [])
        if not str(dc.get("name") or "").strip() and dc.get("sessionId")
    ]
    if not missing:
        job["names_backfilled"] = True
        return
    token = job.get("token")
    if not token:
        return
    job["names_backfilled"] = True
    for dc in missing:
        details, err = fetch_session(token, dc["site"], str(dc.get("sessionId") or ""))
        if err or not details:
            continue
        name = str(details.get("name") or "").strip()
        if name:
            dc["name"] = name


def _dc_eligible_for_save_recovery(dc: dict[str, Any]) -> bool:
    """Only try to recover a saved content ID after an explicit save flow — not after /end."""
    if dc.get("endedWithoutSave"):
        return False
    phase = str(dc.get("phase") or "")
    if phase in {"ended", "ending"}:
        return False
    if phase in {"saving", "save_failed", "shutting_down"}:
        return True
    if dc.get("shutdownResults"):
        return True
    return bool(dc.get("saveConfirmed"))


def _cai_replace_map(job: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for item in (job or {}).get("caiReplaces") or []:
        if not isinstance(item, dict):
            continue
        key = f"{str(item.get('site') or '').lower()}:{str(item.get('savedId') or '').strip()}"
        if key != ":":
            mapping[key] = item
    return mapping


def _cai_integrate_map(job: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for item in (job or {}).get("caiIntegrates") or []:
        if not isinstance(item, dict):
            continue
        key = f"{str(item.get('site') or '').lower()}:{str(item.get('savedId') or '').strip()}"
        if key != ":":
            mapping[key] = item
    for item in _managed_saved_state().get("integrates") or []:
        if not isinstance(item, dict):
            continue
        key = f"{str(item.get('site') or '').lower()}:{str(item.get('savedId') or '').strip()}"
        if key != ":" and key not in mapping:
            mapping[key] = item
    return mapping


def _camgr_transfer_map(job: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for item in (job or {}).get("camgrTransfers") or []:
        if not isinstance(item, dict):
            continue
        if not _camgr_owner_is_mine(str(item.get("owner") or ""), allow_empty=True):
            continue
        key = f"{str(item.get('site') or '').lower()}:{str(item.get('savedId') or '').strip()}"
        if key != ":":
            mapping[key] = item
    for item in _managed_saved_state().get("transfers") or []:
        if not isinstance(item, dict):
            continue
        if not _camgr_owner_is_mine(str(item.get("owner") or ""), allow_empty=True):
            continue
        key = f"{str(item.get('site') or '').lower()}:{str(item.get('savedId') or '').strip()}"
        if key != ":" and key not in mapping:
            mapping[key] = item
    return mapping


def _empty_managed_state() -> dict[str, Any]:
    return {"rows": [], "hidden": [], "transfers": [], "integrates": [], "templates": []}


def _managed_saved_state() -> dict[str, Any]:
    with _managed_saved_lock:
        if not MANAGED_SAVED_IDS_FILE.is_file():
            return _empty_managed_state()
        try:
            data = json.loads(MANAGED_SAVED_IDS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return _empty_managed_state()
        if not isinstance(data, dict):
            return _empty_managed_state()
        rows = [row for row in (data.get("rows") or []) if isinstance(row, dict)]
        hidden = [str(item) for item in (data.get("hidden") or []) if str(item).strip()]
        transfers = [row for row in (data.get("transfers") or []) if isinstance(row, dict)]
        integrates = [row for row in (data.get("integrates") or []) if isinstance(row, dict)]
        templates = [row for row in (data.get("templates") or []) if isinstance(row, dict)]
        return {
            "rows": rows,
            "hidden": hidden,
            "transfers": transfers,
            "integrates": integrates,
            "templates": templates,
        }


def _persist_managed_saved_state(state: dict[str, Any]) -> None:
    payload = {
        "rows": [row for row in (state.get("rows") or []) if isinstance(row, dict)],
        "hidden": [str(item) for item in (state.get("hidden") or []) if str(item).strip()],
        "transfers": [row for row in (state.get("transfers") or []) if isinstance(row, dict)],
        "integrates": [row for row in (state.get("integrates") or []) if isinstance(row, dict)],
        "templates": [row for row in (state.get("templates") or []) if isinstance(row, dict)],
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with _managed_saved_lock:
        try:
            MANAGED_SAVED_IDS_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass


def _managed_hidden_set(state: dict[str, Any] | None = None) -> set[str]:
    data = state if isinstance(state, dict) else _managed_saved_state()
    hidden: set[str] = set()
    for item in data.get("hidden") or []:
        key = str(item or "").strip().lower()
        if key and key != ":":
            hidden.add(key)
    return hidden


def _normalize_saved_id_row(item: dict[str, Any]) -> dict[str, Any] | None:
    site = str(item.get("site") or "").strip().lower()
    saved_id = str(item.get("savedId") or item.get("saved_id") or item.get("contentId") or "").strip()
    if not site or not saved_id:
        return None
    parent = str(item.get("parentId") or item.get("parent_id") or "").strip()
    published = str(item.get("publishedId") or item.get("published_id") or parent or "").strip()
    if published == saved_id:
        published = ""
    if parent == saved_id:
        parent = ""
    return {
        "site": site,
        "savedId": saved_id,
        "publishedId": published,
        "name": str(item.get("name") or "").strip(),
        "parentId": parent or published,
        "sourceDemoId": str(item.get("sourceDemoId") or "").strip(),
        "sessionId": str(item.get("sessionId") or "").strip(),
        # ContentDEV rows are a vPod, not saved content, so there is no topology to open.
        "contentViewUrl": str(item.get("contentViewUrl") or "").strip()
        or ("" if is_cdev_camgr_guid(site) else _content_view_url(site, saved_id)),
        "publishedLookupDone": bool(item.get("publishedLookupDone")),
    }


def _lookup_published_id(site: str, saved_id: str) -> str:
    saved = str(saved_id or "").strip()
    site_code = (site or "").strip().lower()
    if not site_code or not saved:
        return ""
    token = _cached_user_access_token()
    if token:
        details = fetch_content(token, site_code, saved)
        parent = extract_parent_content_id(details or {}, saved_id=saved)
        if parent:
            return parent
    cookie = _camgr_cookie()
    if cookie:
        demo = fetch_camgr_demo(cookie, site_code, saved)
        if demo.get("ok"):
            parent = str(demo.get("rootDemoId") or "").strip()
            if parent.isdigit() and parent != saved:
                return parent
    return ""


def _ensure_published_id(site: str, saved_id: str, current: str = "") -> str:
    saved = str(saved_id or "").strip()
    have = str(current or "").strip()
    if have and have != saved:
        return have
    return _lookup_published_id(site, saved)


def _upsert_managed_saved_rows(items: list[dict[str, Any]]) -> int:
    state = _managed_saved_state()
    by_key: dict[str, dict[str, Any]] = {}
    for row in state.get("rows") or []:
        norm = _normalize_saved_id_row(row)
        if not norm:
            continue
        by_key[_saved_id_key(norm["site"], norm["savedId"])] = norm
    hidden = _managed_hidden_set(state)
    added = 0
    changed = False
    for item in items:
        norm = _normalize_saved_id_row(item)
        if not norm:
            continue
        key = _saved_id_key(norm["site"], norm["savedId"])
        if key in hidden:
            hidden.discard(key)
            changed = True
        if key not in by_key:
            added += 1
            changed = True
        existing = by_key.get(key) or {}
        merged = {**existing, **{k: v for k, v in norm.items() if v}}
        if merged != existing:
            changed = True
        by_key[key] = merged
    if not changed:
        return added
    state["rows"] = list(by_key.values())
    state["hidden"] = sorted(hidden)
    _persist_managed_saved_state(state)
    return added


def _hide_managed_saved_ids(items: list[CaiDemoRef]) -> int:
    state = _managed_saved_state()
    hidden = _managed_hidden_set(state)
    by_key: dict[str, dict[str, Any]] = {}
    for row in state.get("rows") or []:
        norm = _normalize_saved_id_row(row)
        if not norm:
            continue
        by_key[_saved_id_key(norm["site"], norm["savedId"])] = norm
    added = 0
    for item in items:
        key = _saved_id_key(item.site, item.saved_id)
        if not key or key == ":":
            continue
        if key not in hidden:
            hidden.add(key)
            added += 1
        by_key.pop(key, None)
    state["rows"] = list(by_key.values())
    state["hidden"] = sorted(hidden)
    _persist_managed_saved_state(state)
    return added


def _upsert_camgr_transfer(job: dict[str, Any] | None, item: dict[str, Any]) -> dict[str, Any]:
    site = str(item.get("site") or "").strip().lower()
    saved_id = str(item.get("savedId") or "").strip()
    key = _saved_id_key(site, saved_id)
    if job is not None:
        rows = list(job.get("camgrTransfers") or [])
        updated = False
        for row in rows:
            if _saved_id_key(str(row.get("site") or ""), str(row.get("savedId") or "")) == key:
                row.update(item)
                updated = True
                break
        if not updated:
            rows.append(item)
        job["camgrTransfers"] = rows
    state = _managed_saved_state()
    transfers = [row for row in (state.get("transfers") or []) if isinstance(row, dict)]
    updated = False
    for row in transfers:
        if _saved_id_key(str(row.get("site") or ""), str(row.get("savedId") or "")) == key:
            row.update(item)
            updated = True
            break
    if not updated:
        transfers.append(item)
    state["transfers"] = transfers
    _persist_managed_saved_state(state)
    return item


def _saved_id_hidden_set(job: dict[str, Any] | None) -> set[str]:
    hidden: set[str] = set()
    for item in (job or {}).get("savedIdHidden") or []:
        if isinstance(item, str):
            key = item.strip().lower()
        elif isinstance(item, dict):
            key = (
                f"{str(item.get('site') or '').strip().lower()}:"
                f"{str(item.get('savedId') or item.get('saved_id') or '').strip()}"
            )
        else:
            continue
        if key and key != ":":
            hidden.add(key)
    hidden |= _managed_hidden_set()
    return hidden


def _saved_id_key(site: str, saved_id: str) -> str:
    return f"{str(site or '').strip().lower()}:{str(saved_id or '').strip()}"


def _hide_saved_ids(job: dict[str, Any] | None, items: list[CaiDemoRef]) -> int:
    before = _saved_id_hidden_set(job)
    if job is not None:
        hidden = [str(item) for item in (job.get("savedIdHidden") or []) if isinstance(item, str)]
        seen = set(k.strip().lower() for k in hidden)
        for item in items:
            key = _saved_id_key(item.site, item.saved_id)
            if not key or key == ":" or key in seen:
                continue
            hidden.append(key)
            seen.add(key)
        job["savedIdHidden"] = hidden
    _hide_managed_saved_ids(items)
    after = _saved_id_hidden_set(job)
    return max(0, len(after) - len(before))


def _saved_id_display_row(
    *,
    site: str,
    saved_id: str,
    published_id: str = "",
    name: str = "",
    session_id: str = "",
    source_demo_id: str = "",
    parent_id: str = "",
    content_view_url: str = "",
    replace: dict[str, Any] | None = None,
    transfer: dict[str, Any] | None = None,
    integrate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    replace = replace or {}
    transfer = transfer or {}
    integrate = integrate or {}
    status = str(replace.get("status") or "").strip().lower()
    replace_vm_chips = [
        dict(chip) for chip in (replace.get("vmTasks") or []) if isinstance(chip, dict)
    ]
    if not replace_vm_chips and replace.get("vms"):
        # Rows saved before per-VM tracking, so fall back to the request's status.
        replace_vm_chips = cai_replace_vm_chips(
            list(replace.get("vms") or []),
            [],
            saved_id=saved_id,
            target_id=str(replace.get("targetId") or ""),
            overall=status,
        )
    transfer_status = str(transfer.get("statusRaw") or transfer.get("status") or "").strip()
    progress = transfer.get("progress")
    try:
        progress_n = int(progress)
    except (TypeError, ValueError):
        progress_n = None
    transfer_label = transfer_status
    if transfer_label and progress_n is not None and transfer_label.upper() not in {"COMPLETE", "ERROR"}:
        transfer_label = f"{transfer_label} {progress_n}%"
    dests = []
    seen_dests: set[str] = set()
    for raw in transfer.get("dcs") or []:
        text = str(raw or "").strip()
        if ":" in text:
            text = text.rsplit(":", 1)[0]
        key = text.upper()
        if not key or key in seen_dests:
            continue
        seen_dests.add(key)
        dests.append(text)
    chips = camgr_dc_status_chips(dests, list(transfer.get("dcStatus") or []), overall=transfer_status)
    if transfer_label and dests and not chips:
        transfer_label = f"{transfer_label} → {', '.join(dests)}"
    integrate_status = str(integrate.get("status") or "").strip().lower()
    integrate_chips = [
        dict(chip) if isinstance(chip, dict) else chip
        for chip in (integrate.get("dcTasks") or [])
    ]
    for chip in integrate_chips:
        if not isinstance(chip, dict):
            continue
        href = str(chip.get("href") or "")
        if href and TBV3_UI not in href:
            chip["href"] = ""
    if not integrate_chips:
        integrate_chips = cai_integrate_dc_chips(
            list(integrate.get("dcs") or []),
            [],
            overall=integrate_status,
            previous=list(integrate.get("dcTasks") or []),
        )
    integrate_label = ""
    if integrate_status:
        dest_labels = [cai_dc_label(dc) for dc in (integrate.get("dcs") or []) if str(dc).strip()]
        integrate_label = f"Integration {integrate_status}"
        if dest_labels and not integrate_chips:
            integrate_label += f" → {', '.join(dest_labels)}"
        chip_ids = [str(chip.get("newId") or "").strip() for chip in integrate_chips if str(chip.get("newId") or "").strip()]
        dest_count = len([dc for dc in (integrate.get("dcs") or []) if str(dc).strip()])
        if not chip_ids and dest_count <= 1:
            new_id = str(integrate.get("newId") or "").strip()
            if new_id and " " not in new_id and "," not in new_id:
                integrate_label += f" · {new_id}"
    burn_in_status = str(integrate.get("burnInStatus") or "").strip().lower()
    if burn_in_status:
        burn_labels = {
            "pending": "burn-in pending",
            "waiting_id": "burn-in waiting for demo IDs",
            "waiting_auth": "burn-in waiting for dCloud login",
            "queued": "burn-in scheduled",
            "error": "burn-in error",
        }
        integrate_label += (
            (" · " if integrate_label else "")
            + burn_labels.get(burn_in_status, f"burn-in {burn_in_status}")
        )
    auto_status = str(transfer.get("autoIntegrateStatus") or "").strip().lower()
    auto_pending = (
        bool(transfer.get("autoIntegrate"))
        and str(transfer.get("status") or "").strip().lower() == "complete"
        and auto_status in {"", "pending", "waiting"}
    )
    return {
        "site": site.upper(),
        "savedId": saved_id,
        "publishedId": published_id,
        "name": name,
        "sessionId": session_id,
        "sourceDemoId": source_demo_id,
        "parentId": parent_id or published_id,
        "contentViewUrl": content_view_url or _content_view_url(site, saved_id),
        "caiUrl": cai_demo_url(site, saved_id),
        "camgrUrl": CAMGR_HOME,
        "replaceStatus": status,
        "replaceNewId": str(replace.get("newId") or ""),
        "replaceMessage": str(replace.get("message") or ""),
        "replaceVms": [str(vm) for vm in (replace.get("vms") or [])],
        "replaceVmStatus": replace_vm_chips,
        "transferStatus": str(transfer.get("status") or "").strip().lower(),
        "transferStatusLabel": transfer_label,
        "transferGuid": str(transfer.get("guid") or ""),
        "transferProgress": progress_n if progress_n is not None else 0,
        "transferDcs": list(transfer.get("dcs") or []),
        "transferDcStatus": chips,
        "transferMessage": str(transfer.get("message") or ""),
        "integrateStatus": str(integrate.get("status") or "").strip().lower(),
        "integrateStatusLabel": integrate_label,
        "integrateNewId": str(integrate.get("newId") or ""),
        "integrateDcs": list(integrate.get("dcs") or []),
        "integrateDcStatus": integrate_chips,
        "integrateMessage": str(integrate.get("message") or ""),
        "integrateIdsChecked": bool(integrate.get("dcIdsChecked")),
        "autoIntegrate": bool(transfer.get("autoIntegrate")),
        "autoIntegratePending": auto_pending,
        "autoIntegrateStatus": auto_status,
        "autoBurnIn": bool(integrate.get("autoBurnIn") or transfer.get("autoBurnIn")),
        "burnInDays": int(integrate.get("burnInDays") or transfer.get("burnInDays") or 1),
        "burnInStatus": burn_in_status or str(transfer.get("burnInStatus") or ""),
        "burnInJobId": str(integrate.get("burnInJobId") or ""),
        "burnInMessage": str(integrate.get("burnInMessage") or ""),
    }


def _saved_id_summary(job: dict[str, Any] | None = None, *, hide_completed: bool = False) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    by_site: dict[str, str] = {}
    replaces = _cai_replace_map(job)
    transfers = _camgr_transfer_map(job)
    integrates = _cai_integrate_map(job)
    for item in list(integrates.values()):
        if not isinstance(item, dict):
            continue
        chips = list(item.get("dcTasks") or [])
        if chips and _attach_tbv3_chip_links(chips):
            item["dcTasks"] = chips
            _upsert_cai_integrate(job, item)
    hidden = _saved_id_hidden_set(job)
    seen: set[str] = set()
    auto_add: list[dict[str, Any]] = []
    backfill: list[dict[str, Any]] = []
    for raw in _managed_saved_state().get("rows") or []:
        norm = _normalize_saved_id_row(raw)
        if not norm:
            continue
        if norm.get("publishedId") or norm.get("publishedLookupDone"):
            continue
        found = _ensure_published_id(norm["site"], norm["savedId"], "")
        backfill.append(
            {
                **norm,
                "publishedId": found,
                "parentId": found or str(norm.get("parentId") or ""),
                "publishedLookupDone": True,
            }
        )
    if backfill:
        _upsert_managed_saved_rows(backfill)

    for dc in (job or {}).get("dcs") or []:
        if str(dc.get("phase") or "") != "saved" or not dc.get("saveConfirmed"):
            continue
        saved_id = str(dc.get("savedId") or "").strip()
        if not saved_id:
            continue
        site = str(dc.get("site") or "").strip().lower()
        key = _saved_id_key(site, saved_id)
        if key in hidden:
            continue
        auto_add.append(
            {
                "site": site,
                "savedId": saved_id,
                "publishedId": str(dc.get("savedParentId") or dc.get("demoId") or "").strip(),
                "name": str(dc.get("savedName") or dc.get("name") or "").strip(),
                "sessionId": dc.get("sessionId") or "",
                "sourceDemoId": dc.get("demoId") or "",
                "parentId": dc.get("savedParentId") or dc.get("demoId") or "",
                "contentViewUrl": _content_view_url(site, saved_id),
            }
        )

    if auto_add:
        _upsert_managed_saved_rows(auto_add)

    sources: list[dict[str, Any]] = list(auto_add)
    for row in _managed_saved_state().get("rows") or []:
        sources.append(row)

    for raw in sources:
        norm = _normalize_saved_id_row(raw)
        if not norm:
            continue
        site = norm["site"]
        saved_id = norm["savedId"]
        key = _saved_id_key(site, saved_id)
        if key in hidden or key in seen:
            continue
        replace = replaces.get(key) or {}
        status = str(replace.get("status") or "").strip().lower()
        if hide_completed and status == "completed":
            continue
        seen.add(key)
        by_site[site] = saved_id
        rows.append(
            _saved_id_display_row(
                site=site,
                saved_id=saved_id,
                published_id=norm.get("publishedId") or "",
                name=norm.get("name") or "",
                session_id=str(norm.get("sessionId") or ""),
                source_demo_id=str(norm.get("sourceDemoId") or ""),
                parent_id=str(norm.get("parentId") or ""),
                content_view_url=str(norm.get("contentViewUrl") or ""),
                replace=replace,
                transfer=transfers.get(key) or {},
                integrate=integrates.get(key) or {},
            )
        )
    rows.sort(key=lambda row: (str(row.get("site") or ""), str(row.get("savedId") or "")))
    return {
        "bySite": by_site,
        "rows": rows,
    }


def _persist_saved_ids(job: dict[str, Any] | None = None) -> None:
    summary = _saved_id_summary(job)
    if not summary["rows"]:
        return
    path = APP_DIR / "last-saved-ids.json"
    payload = {
        "jobId": (job or {}).get("id") or "",
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **summary,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _maybe_job(job_id: str) -> dict[str, Any] | None:
    jid = str(job_id or "").strip()
    if not jid:
        return None
    try:
        return _job(jid)
    except HTTPException:
        return None


def _job_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job.get("id") or "",
        "phase": job.get("phase") or "",
        "createdAt": job.get("createdAt") or 0,
        "log": list(job.get("log") or [])[-2000:],
        "error": job.get("error") or "",
        "token_source": job.get("token_source") or "",
        "selected_vms": job.get("selected_vms") or [],
        "dcs": job.get("dcs") or [],
        "contentExport": bool(job.get("contentExport", True)),
        "caiReplaces": list(job.get("caiReplaces") or []),
        "caiIntegrates": list(job.get("caiIntegrates") or []),
        "camgrTransfers": list(job.get("camgrTransfers") or []),
        "savedIdHidden": list(job.get("savedIdHidden") or []),
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _persist_job(job: dict[str, Any]) -> None:
    if not job.get("id"):
        return
    # Threads and in-flight requests hold their own reference to a job, so a cleared
    # one has to refuse to write itself back out.
    if job.get("discarded"):
        return
    if not job.get("dcs"):
        _unlink_last_job(str(job.get("id") or ""))
        return
    try:
        LAST_JOB_FILE.write_text(
            json.dumps(_job_snapshot(job), indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return


def _unlink_last_job(job_id: str | None = None) -> None:
    if not LAST_JOB_FILE.is_file():
        return
    if job_id:
        try:
            data = json.loads(LAST_JOB_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        stored_id = str(data.get("id") or "")
        if stored_id and stored_id != job_id:
            return
    try:
        LAST_JOB_FILE.unlink()
    except OSError:
        return


def _content_view_url(
    site: str,
    content_id: str,
    content: dict[str, Any] | None = None,
) -> str:
    return edit_topology_url(site, content_id, content)


def _session_display_name(details: dict[str, Any] | None) -> str:
    if not isinstance(details, dict):
        return ""
    nested = [details]
    for key in ("sessionDetails", "session"):
        value = details.get(key)
        if isinstance(value, dict):
            nested.append(value)
    for obj in nested:
        for field in ("name", "parentDemoName"):
            text = str(obj.get(field) or "").strip()
            if text:
                return text
    return ""


def _dc_ids_from_session(details: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(details, dict):
        return {}
    fields: dict[str, str] = {}
    active_id = session_saved_content_id(details)
    if active_id:
        fields["activeId"] = active_id
    parent = str(details.get("parentId") or details.get("parentDemoId") or "").strip()
    if parent:
        fields["savedParentId"] = parent
    name = _session_display_name(details)
    if name:
        fields["name"] = name
    owner = session_owner(details)
    if owner:
        fields["owner"] = owner
    fields.update(_session_schedule_fields(details))
    return fields


def _find_dc(
    job: dict[str, Any],
    site: str,
    session_id: str | None = None,
    demo_id: str | None = None,
) -> dict[str, Any] | None:
    site_code = (site or "").strip().lower()
    dcs = [dc for dc in (job.get("dcs") or []) if dc.get("site") == site_code]
    if session_id:
        sid = str(session_id).strip()
        hit = next((dc for dc in dcs if str(dc.get("sessionId") or "") == sid), None)
        if hit:
            return hit
    if demo_id is not None:
        did = str(demo_id).strip()
        hits = [dc for dc in dcs if str(dc.get("demoId") or "") == did]
        if hits:
            pending = next(
                (dc for dc in hits if not str(dc.get("sessionId") or "").strip()),
                None,
            )
            return pending or hits[-1]
    return dcs[0] if dcs else None


def _dc_needs_schedule(dc: dict[str, Any]) -> bool:
    if str(dc.get("sessionId") or "").strip():
        return False
    phase = str(dc.get("phase") or "")
    if phase in {
        "saved",
        "ended",
        "error",
        "ready",
        "save_failed",
        "shutting_down",
        "saving",
        "powering",
        "waiting",
    }:
        return False
    return True


def _dc_wanted(dc: dict[str, Any], wanted_sites: set[str], session_id: str = "") -> bool:
    if wanted_sites and dc.get("site") not in wanted_sites:
        return False
    if session_id and str(dc.get("sessionId") or "") != session_id:
        return False
    return True


def _session_ref_pairs(sessions: list[SessionRef]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for ref in sessions:
        site = (ref.site or "").strip().lower()
        sid = str(ref.session_id or "").strip()
        if site in SITES and sid:
            pairs.append((site, sid))
    return pairs


def _dc_matches_action(
    dc: dict[str, Any],
    *,
    session_pairs: list[tuple[str, str]] | None,
    wanted_sites: set[str],
    wanted_sid: str,
) -> bool:
    if session_pairs is not None:
        site = str(dc.get("site") or "").lower()
        sid = str(dc.get("sessionId") or "").strip()
        return (site, sid) in session_pairs
    return _dc_wanted(dc, wanted_sites, wanted_sid)


def _save_session_key(dc: dict[str, Any]) -> tuple[str, str]:
    return (str(dc.get("site") or "").lower(), str(dc.get("sessionId") or "").strip())


def _dc_save_retry_only(dc: dict[str, Any]) -> bool:
    phase = str(dc.get("phase") or "")
    if phase == "save_failed":
        return True
    return phase == "error" and "save failed" in str(dc.get("message") or "").lower()


def _dc_activity_ts(job: dict[str, Any], dc: dict[str, Any]) -> float:
    """When this card was last touched, falling back to the job it belongs to."""
    try:
        touched = float(dc.get("touchedAt") or 0)
    except (TypeError, ValueError):
        touched = 0.0
    return touched or _job_activity_ts(job)


def _dc_too_old_for_bulk(job: dict[str, Any], dc: dict[str, Any]) -> bool:
    activity = _dc_activity_ts(job, dc)
    if activity <= 0:
        return False
    return (time.time() - activity) > _BULK_SAVE_MAX_AGE_SECS


def _dc_can_save(dc: dict[str, Any]) -> bool:
    if not str(dc.get("sessionId") or "").strip():
        return False
    if dc.get("saveConfirmed"):
        return False
    phase = str(dc.get("phase") or "")
    if phase in {"saved", "ended", "ending", "saving", "shutting_down"}:
        return False
    if phase == "ready":
        return True
    return _dc_save_retry_only(dc)


def _save_name_for_dc(payload: ShutdownPayload, dc: dict[str, Any]) -> str:
    """Per-session save name when the batch covers several demos, else the shared name."""
    site = str(dc.get("site") or "").strip().lower()
    sid = str(dc.get("sessionId") or "").strip()
    for row in payload.save_names or []:
        name = str(row.name or "").strip()
        if not name:
            continue
        if str(row.site or "").strip().lower() == site and str(row.session_id or "").strip() == sid:
            return name
    return payload.save_name


def _save_description_for_dc(payload: ShutdownPayload, dc: dict[str, Any]) -> str:
    site = str(dc.get("site") or "").strip().lower()
    sid = str(dc.get("sessionId") or "").strip()
    for row in payload.save_names or []:
        description = str(row.description or "").strip()
        if not description:
            continue
        if str(row.site or "").strip().lower() == site and str(row.session_id or "").strip() == sid:
            return description
    return (payload.save_description or "").strip() or "Saved"


def _job_identities(job: dict[str, Any]) -> set[str]:
    return token_identities(str(job.get("token") or ""))


def _annotate_dc_ownership(job: dict[str, Any]) -> None:
    """Tell the UI which cards are mine so it can gate save and warn before end."""
    identities = _job_identities(job)
    for dc in job.get("dcs") or []:
        dc["ownedByMe"] = owner_is_me(str(dc.get("owner") or ""), identities)
        dc["staleForBulk"] = _dc_too_old_for_bulk(job, dc)


def _dc_owned_by_me(job: dict[str, Any], dc: dict[str, Any]) -> bool | None:
    """True/False when the session owner and my token are both known, None otherwise."""
    return owner_is_me(str(dc.get("owner") or ""), _job_identities(job))


def _not_my_sessions_message(dcs: list[dict[str, Any]], action: str) -> str:
    labels = [
        f"{str(dc.get('site') or '').upper()} {dc.get('sessionId') or ''}"
        f" (owner {dc.get('owner') or 'unknown'})"
        for dc in dcs
    ]
    return (
        f"You do not own {', '.join(labels)}, so this tool will not {action} it. "
        "Ask the owner, or work from your own session."
    )


def _claim_dc_for_save(job: dict[str, Any], dc: dict[str, Any]) -> bool:
    """One in-flight save per live session — blocks individual + bulk from double-saving."""
    with _jobs_lock:
        if not _dc_can_save(dc):
            return False
        key = _save_session_key(dc)
        if not key[1]:
            return False
        claimed = job.setdefault("_save_claimed_sessions", set())
        if key in claimed:
            return False
        claimed.add(key)
        retry = _dc_save_retry_only(dc)
        dc["_save_claimed"] = True
        dc["_save_retry_only"] = retry
        if retry:
            dc["phase"] = "saving"
            dc["message"] = "Saving session…"
        else:
            dc["phase"] = "shutting_down"
            dc["message"] = "Guest shutdown…"
        return True


def _release_save_claim(job: dict[str, Any], dc: dict[str, Any]) -> None:
    with _jobs_lock:
        claimed = job.get("_save_claimed_sessions")
        if isinstance(claimed, set):
            claimed.discard(_save_session_key(dc))
        dc.pop("_save_claimed", None)
        dc.pop("_save_retry_only", None)


def _iter_save_candidates(job: dict[str, Any], payload: ShutdownPayload):
    wanted = {site.strip().lower() for site in payload.sites if site.strip()}
    wanted_sid = str(payload.session_id or "").strip()
    session_pairs = _session_ref_pairs(payload.sessions)
    use_pairs = session_pairs if session_pairs else None
    for dc in job.get("dcs") or []:
        if not _dc_matches_action(
            dc,
            session_pairs=use_pairs,
            wanted_sites=wanted,
            wanted_sid=wanted_sid,
        ):
            continue
        # Monitoring cards are never part of a bulk save, however they were targeted.
        if dc.get("monitorOnly") and not payload.single_card:
            continue
        # Nor is a card left over from an earlier run.
        if not payload.single_card and _dc_too_old_for_bulk(job, dc):
            continue
        if not dc.get("sessionId"):
            continue
        yield dc


def _session_schedule_fields(details: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(details, dict):
        return {}
    fields: dict[str, str] = {}
    start = str(details.get("start") or "").strip()
    stop = str(details.get("stop") or "").strip()
    if start:
        fields["scheduleStart"] = start
    if stop:
        fields["scheduleStop"] = stop
    return fields


def _mark_dc_saved(
    job: dict[str, Any],
    site: str,
    *,
    details: dict[str, Any] | None,
    status: str,
    message: str,
    session_id: str = "",
    save_confirmed: bool = True,
) -> None:
    saved_id = session_saved_content_id(details)
    parent = ""
    name = ""
    dc = _find_dc(job, site, session_id) or {}
    if not saved_id:
        saved_id = str(dc.get("activeId") or dc.get("savedId") or "").strip()
    if details:
        parent = str(details.get("parentId") or details.get("parentDemoId") or "").strip()
        name = str(details.get("name") or "").strip()
    if not parent:
        parent = str(dc.get("savedParentId") or dc.get("demoId") or "").strip()
    if not name:
        name = str(dc.get("savedName") or dc.get("name") or "").strip()
    content = None
    shared_with: list[dict[str, str]] = []
    if saved_id:
        token = job.get("token")
        if token:
            shared_with, _ = fetch_content_shared_with(token, site, saved_id)
            content = fetch_content(token, site, saved_id)
        if content:
            name = str(content.get("name") or name).strip()
    fields: dict[str, Any] = {
        "phase": "saved",
        "status": status,
        "savedId": saved_id,
        "savedParentId": parent,
        "savedName": name,
        "contentViewUrl": _content_view_url(site, saved_id, content),
        "sharedWith": shared_with,
        "vms": [],
        "message": message,
        "savePending": False,
    }
    if save_confirmed:
        fields["saveConfirmed"] = True
    _set_dc(
        job,
        site,
        match_session=session_id or str(dc.get("sessionId") or ""),
        **fields,
    )
    extra = f" saved ID {saved_id}" if saved_id else ""
    _log(job, f"{site.upper()}: session is saved ({status}).{extra}")


def _recover_saved_from_active_id(
    job: dict[str, Any],
    dc: dict[str, Any],
    token: str,
    *,
    details: dict[str, Any] | None,
    status: str,
) -> bool:
    """After save the session often 404s. The content ID is the session's activeId."""
    site = dc["site"]
    skip = {
        str(dc.get("sessionId") or "").strip(),
        str(dc.get("demoId") or "").strip(),
        str(dc.get("savedParentId") or "").strip(),
    }
    skip = {item for item in skip if item}
    candidates: list[str] = []
    for raw in (
        session_saved_content_id(details),
        dc.get("activeId"),
        dc.get("savedId"),
    ):
        text = str(raw or "").strip()
        if text and text not in skip and text not in candidates:
            candidates.append(text)
    chosen = ""
    content = None
    for cid in candidates:
        content = fetch_content(token, site, cid)
        if content:
            chosen = cid
            break
    if not chosen and candidates:
        chosen = candidates[0]
    if not chosen:
        return False
    extra = dict(details or {})
    extra["activeId"] = chosen
    if content and content.get("name"):
        extra["name"] = content.get("name")
    found = "verified in custom content" if content else "stored activeId (session is gone)"
    _mark_dc_saved(
        job,
        site,
        details=extra,
        status=status,
        message=f"Content ID {chosen} ({found}).",
        session_id=str(dc.get("sessionId") or ""),
    )
    return True


def _dc_power_targets(dc: dict[str, Any], job: dict[str, Any]) -> list[dict[str, Any]]:
    explicit = dc.get("powerOnTargets") or []
    if explicit:
        return explicit
    return job.get("selected_vms") or []


def _load_dc_vms(job: dict[str, Any], dc: dict[str, Any], token: str) -> str | None:
    site = dc["site"]
    sid = str(dc.get("sessionId") or "").strip()
    if not sid:
        return "Session ID is missing."
    vms, details, err = list_session_vms(token, site, sid)
    if err:
        return err
    live, _power_err = apply_tbv3_power_states(token, site, sid, vms, details)
    live = attach_vm_access_links(token, site, sid, live)
    live = tag_selected_vms(live, _dc_power_targets(dc, job))
    extra = _dc_ids_from_session(details)
    if details:
        extra["viewUrl"] = session_view_url(site, sid, session=details)
    _set_dc(job, site, match_session=sid, vms=live, **extra)
    return None


def _vm_is_powered_on(vm: dict[str, Any]) -> bool:
    key = str(vm.get("powerState") or "").lower().replace(" ", "").replace("_", "")
    return key in {state.replace("_", "") for state in POWER_ON_STATES}


def _active_card_message(content_export: bool) -> str:
    """Only exported sessions end in a save, so regular ones skip that hint."""
    if content_export:
        return "Connect to your session, then guest-shutdown & save when finished."
    return "Connect to your session."


def _vm_power_summary(vms: list[dict[str, Any]]) -> str:
    on = sum(1 for vm in vms if _vm_is_powered_on(vm))
    off = len(vms) - on
    return f"{on} powered on, {off} powered off"


def _power_selected_after_interrupt(job: dict[str, Any], dc: dict[str, Any], token: str) -> None:
    """If a reload killed the wait/power thread, finish power-on for selected VMs once."""
    if job.get("worker_alive") or dc.get("autoPowered") or not dc.get("powerOnPending"):
        return
    site = dc["site"]
    sid = str(dc.get("sessionId") or "").strip()
    targets = _dc_power_targets(dc, job)
    chosen, _missing = match_selected_vms(dc.get("vms") or [], targets)
    need = [vm for vm in chosen if not _vm_is_powered_on(vm)]
    if not need:
        _set_dc(job, site, match_session=sid, autoPowered=True, powerOnPending=False)
        return
    _log(
        job,
        f"{site.upper()}: wait thread was interrupted — powering on "
        f"{', '.join(str(vm.get('name') or '') for vm in need)}.",
    )
    results = power_on_vms(token, site, sid, need, progress=lambda msg: _log(job, msg))
    _set_dc(
        job,
        site,
        match_session=sid,
        autoPowered=True,
        powerOnPending=False,
        powerResults=results,
    )
    _load_dc_vms(job, dc, token)


def _refresh_dc_save_progress(job: dict[str, Any], dc: dict[str, Any], token: str) -> None:
    """Poll dCloud while a card is saving — refresh-status used to skip this phase entirely."""
    site = dc["site"]
    sid = str(dc.get("sessionId") or "").strip()
    if not sid:
        return

    def bump(**fields: Any) -> None:
        _set_dc(job, site, match_session=sid, **fields)

    public, _pub_err = check_public_session_status(site, sid)
    details, err = fetch_session(token, site, sid)
    if is_auth_error(err):
        fresh, _refresh_err = _refresh_job_token(job)
        if fresh:
            token = fresh
            job["token"] = fresh
            details, err = fetch_session(token, site, sid)
    if is_auth_error(err):
        bump(message=err or "dCloud token expired.")
        return

    numeric = ""
    if details:
        numeric = details.get("status") or details.get("sessionStatus") or ""
        bump(
            viewUrl=session_view_url(site, sid, session=details),
            **_dc_ids_from_session(details),
        )
    status = format_status(numeric, public)
    session_gone = bool(err and ("not found" in err.lower() or "404" in err))

    # Finish line: dCloud has dropped the live session. End hides the card; save
    # stays visible until this 404 so the user can watch each DC complete.
    if session_gone:
        if _dc_eligible_for_save_recovery(dc) and _recover_saved_from_active_id(
            job, dc, token, details=None, status=public or status or "saved"
        ):
            _persist_saved_ids(job)
            return
        if dc.get("savePending") and dc.get("savedId"):
            _mark_dc_saved(
                job,
                site,
                details=None,
                status=public or "saved",
                message=f"Content ID {dc.get('savedId')} — dCloud finished saving.",
                session_id=sid,
            )
            _persist_saved_ids(job)
            return
        bump(
            phase="ended",
            status="gone",
            vms=[],
            message="Session ended before save finished.",
        )
        return

    if is_failed_status(public) or is_failed_status(numeric):
        if is_stopping_status(public) or is_stopping_status(numeric):
            pass
        elif is_saving_in_progress_status(public) or is_saving_in_progress_status(numeric):
            pass
        elif is_saved_status(public) or is_saved_status(numeric):
            pass
        else:
            bump(
                phase="save_failed",
                status=status,
                message=f"Save failed or session error ({status}).",
            )
            return

    label = status or public or "in progress"
    saved_id = str(dc.get("savedId") or "").strip()
    id_prefix = f"Saved content ID {saved_id} — " if saved_id else ""
    if is_stopping_status(public) or is_stopping_status(numeric):
        bump(
            phase="shutting_down",
            status=label,
            savePending=True,
            message=f"{id_prefix}VMs are shutting down before the save.",
        )
        return
    if is_saved_status(public) or is_saved_status(numeric):
        # SAVED is dCloud's last word on a save. The session can sit on the
        # dashboard for a while afterwards, so finish the card now.
        if not (
            _dc_eligible_for_save_recovery(dc)
            and _recover_saved_from_active_id(job, dc, token, details=details, status=label)
        ):
            _mark_dc_saved(
                job,
                site,
                details=details,
                status=label,
                message=f"{id_prefix}dCloud finished saving.",
                session_id=sid,
            )
        _persist_saved_ids(job)
        return
    if is_saving_in_progress_status(public) or is_saving_in_progress_status(numeric):
        bump(
            phase="saving",
            status=label,
            savePending=True,
            message=f"{id_prefix}dCloud is writing the saved content.",
        )
        return
    bump(
        phase="saving",
        status=label,
        savePending=True,
        message=f"{id_prefix}Waiting for dCloud to report the save.",
    )


def _dc_reset_in_progress(dc: dict[str, Any]) -> bool:
    """True while a reset we know about is still rebuilding the session in dCloud."""
    try:
        until = float(dc.get("resetPendingUntil") or 0)
    except (TypeError, ValueError):
        return False
    return until > time.time()


def _refresh_dc_from_dcloud(job: dict[str, Any], dc: dict[str, Any], token: str) -> None:
    site = dc["site"]
    sid = str(dc.get("sessionId") or "").strip()
    if not sid:
        return
    phase = str(dc.get("phase") or "")
    if phase in {"saving", "shutting_down"}:
        _refresh_dc_save_progress(job, dc, token)
        return
    if dc.get("phase") in _SKIP_REFRESH_DC_PHASES:
        return

    def bump(**fields: Any) -> None:
        _set_dc(job, site, match_session=sid, **fields)

    # dCloud drops the session and rebuilds it under the same ID, so a gap here is expected.
    resetting = _dc_reset_in_progress(dc)

    public, _pub_err = check_public_session_status(site, sid)
    details, err = fetch_session(token, site, sid)
    if is_auth_error(err):
        fresh, _refresh_err = _refresh_job_token(job)
        if fresh:
            token = fresh
            job["token"] = fresh
            details, err = fetch_session(token, site, sid)
    if is_auth_error(err):
        bump(message=err or "dCloud token expired.")
        return
    if err and ("not found" in err.lower() or "404" in err):
        if resetting:
            bump(
                phase="resetting",
                status="reset",
                vms=[],
                message="dCloud has not brought the session back yet.",
            )
            return
        if dc.get("endedWithoutSave") or dc.get("phase") in {"ended", "ending"}:
            bump(
                phase="ended",
                status="gone",
                vms=[],
                message="Session ended (not saved).",
            )
            return
        if _dc_eligible_for_save_recovery(dc) and _recover_saved_from_active_id(
            job, dc, token, details=None, status=public or "gone"
        ):
            return
        bump(
            phase="ended",
            status="gone",
            vms=[],
            message=err,
        )
        return
    numeric = ""
    if details:
        numeric = details.get("status") or details.get("sessionStatus") or ""
        bump(
            viewUrl=session_view_url(site, sid, session=details),
            sharedWith=shared_with_from_details(details),
            canReset=bool(details.get("canReset")),
            **_dc_ids_from_session(details),
        )
    status = format_status(numeric, public)
    if is_stopping_status(public) or is_stopping_status(numeric):
        bump(
            phase="ending",
            status=status,
            vms=[],
            message="dCloud is tearing this session down — no save.",
        )
        return
    if is_saved_status(public) or is_saved_status(numeric):
        if not _recover_saved_from_active_id(job, dc, token, details=details, status=status):
            _mark_dc_saved(
                job,
                site,
                details=details,
                status=status,
                message="Already saved in dCloud — removed from active cards.",
                session_id=sid,
            )
        return
    if is_failed_status(public) or is_failed_status(numeric):
        if resetting:
            bump(
                phase="resetting",
                status=status,
                vms=[],
                message=f"dCloud still reports {status}.",
            )
            return
        if dc.get("endedWithoutSave") or dc.get("phase") in {"ended", "ending"}:
            bump(
                phase="ended",
                status=status,
                vms=[],
                message="Session ended (not saved).",
            )
            return
        if _dc_eligible_for_save_recovery(dc) and _recover_saved_from_active_id(
            job, dc, token, details=details, status=status
        ):
            return
        bump(
            phase="ended",
            status=status,
            vms=[],
            message=f"dCloud reports {status}.",
        )
        return
    if is_active_status(public) or is_active_status(numeric):
        vm_err = _load_dc_vms(job, dc, token)
        message = _active_card_message(bool(dc.get("contentExport", True)))
        if vm_err:
            message = f"VMs did not load: {vm_err}"
        elif not (dc.get("vms") or []):
            message = "dCloud returned no VMs for this session."
        elif dc.get("powerOnPending"):
            _power_selected_after_interrupt(job, dc, token)
        bump(
            phase="ready",
            status="Active",
            message=message,
            resetPendingUntil=0,
        )
        return
    # The session is answering again, so a reset we were waiting on has landed.
    bump(
        phase="waiting",
        status=status,
        message="Waiting for dCloud to bring this session up.",
        resetPendingUntil=0,
    )


def _sync_job_phase(job: dict[str, Any]) -> None:
    phases = {str(dc.get("phase") or "") for dc in job.get("dcs") or []}
    if not phases:
        return
    if phases <= {"saved", "ended", "error"}:
        job["phase"] = "complete" if "saved" in phases else "ended"
        return
    if any(phase in {"shutting_down", "saving"} for phase in phases):
        job["phase"] = "shutting_down"
        return
    if "ending" in phases:
        job["phase"] = "ending"
        return
    if phases & {"waiting", "powering", "queued", "scheduling", "resetting"}:
        job["phase"] = "waiting_active"
        return
    if "ready" in phases:
        job["phase"] = "ready_to_patch"
        return


def _refresh_job_from_dcloud(job: dict[str, Any], token: str) -> None:
    job["token"] = token
    for dc in list(job.get("dcs") or []):
        _refresh_dc_from_dcloud(job, dc, token)
    _sync_job_phase(job)


def _watch_job_statuses(job: dict[str, Any]) -> None:
    token = job.get("token") or ""
    while not job["stop"].is_set():
        pending = [
            dc for dc in (job.get("dcs") or []) if dc.get("phase") in _WATCHED_DC_PHASES
        ]
        if not pending:
            _sync_job_phase(job)
            return
        tok = job.get("token") or token
        if tok:
            for dc in pending:
                if job["stop"].is_set():
                    return
                _refresh_dc_from_dcloud(job, dc, tok)
        _sync_job_phase(job)
        time.sleep(20)


def _dismiss_job(job_id: str) -> dict[str, Any]:
    """Drop local job cards. Does not end or save anything in dCloud."""
    with _jobs_lock:
        job = _jobs.pop(job_id, None)
    if job:
        _discard_job(job)
    _unlink_last_job(job_id)
    return {"ok": True, "cleared": True, "id": job_id}


def _parse_updated_at_ts(raw: Any) -> float:
    text = str(raw or "").strip()
    if not text:
        return 0.0
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _job_activity_ts(job: dict[str, Any]) -> float:
    updated = _parse_updated_at_ts(job.get("updatedAt"))
    if updated:
        return updated
    try:
        return float(job.get("createdAt") or 0)
    except (TypeError, ValueError):
        return 0.0


def _read_last_job_snapshot() -> dict[str, Any] | None:
    if not LAST_JOB_FILE.is_file():
        return None
    try:
        data = json.loads(LAST_JOB_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("id") or not data.get("dcs"):
        return None
    return data


def _job_has_live_cards(snapshot: dict[str, Any]) -> bool:
    for dc in snapshot.get("dcs") or []:
        if str(dc.get("phase") or "") not in _TERMINAL_DC_PHASES:
            return True
    return False


def _auto_restore_eligible(snapshot: dict[str, Any]) -> bool:
    activity = _job_activity_ts(snapshot)
    if activity <= 0:
        return False
    age = time.time() - activity
    if age < 0 or age > _AUTO_RESTORE_MAX_AGE_SECS:
        return False
    return _job_has_live_cards(snapshot)


def _last_job_preview() -> dict[str, Any]:
    snapshot = _read_last_job_snapshot()
    if not snapshot:
        raise HTTPException(404, "No saved job to restore.")
    activity = _job_activity_ts(snapshot)
    age = max(0.0, time.time() - activity) if activity else None
    return {
        "available": True,
        "id": str(snapshot.get("id") or ""),
        "phase": str(snapshot.get("phase") or ""),
        "updatedAt": snapshot.get("updatedAt") or "",
        "ageSeconds": int(age) if age is not None else None,
        "autoRestore": _auto_restore_eligible(snapshot),
        "autoRestoreMaxAgeHours": _AUTO_RESTORE_HOURS,
        "hasLiveCards": _job_has_live_cards(snapshot),
    }


def _hydrate_job(data: dict[str, Any]) -> dict[str, Any]:
    data = dict(data)
    data.pop("token", None)
    return {
        "id": str(data.get("id") or ""),
        "phase": str(data.get("phase") or "ready_to_patch"),
        "createdAt": data.get("createdAt") or time.time(),
        "updatedAt": data.get("updatedAt") or "",
        "log": list(data.get("log") or []),
        "error": str(data.get("error") or ""),
        "stop": threading.Event(),
        "auth_resume": threading.Event(),
        "auth_lock": threading.Lock(),
        "auth_needed": False,
        "auth_message": "",
        "token": None,
        "token_source": str(data.get("token_source") or "browser"),
        "selected_vms": data.get("selected_vms") or [],
        "dcs": data.get("dcs") or [],
        "contentExport": bool(data.get("contentExport", True)),
        "caiReplaces": list(data.get("caiReplaces") or []),
        "caiIntegrates": list(data.get("caiIntegrates") or []),
        "camgrTransfers": list(data.get("camgrTransfers") or []),
        "savedIdHidden": list(data.get("savedIdHidden") or []),
    }


def _prune_old_cards(job: dict[str, Any]) -> int:
    """Drop unfinished cards left over from an earlier day, so nothing stale can be saved.

    Saved and ended cards stay: they cannot be saved again and the saved content list is
    built from them. A live session that gets dropped is not lost either — dCloud is the
    record and session monitoring finds it again.
    """
    keep: list[dict[str, Any]] = []
    dropped = 0
    for dc in job.get("dcs") or []:
        activity = _dc_activity_ts(job, dc)
        stale = activity > 0 and (time.time() - activity) > _FINISHED_CARD_KEEP_SECS
        if stale and str(dc.get("phase") or "") not in _TERMINAL_DC_PHASES:
            dropped += 1
            continue
        keep.append(dc)
    if dropped:
        job["dcs"] = keep
        _log(job, f"Cleared {dropped} unfinished card(s) left over from an earlier run.")
    return dropped


def _load_last_job() -> None:
    data = _read_last_job_snapshot()
    if not data:
        return
    job = _hydrate_job(data)
    if not job["id"]:
        return
    if _prune_old_cards(job):
        _persist_job(job)
    with _jobs_lock:
        _jobs.setdefault(job["id"], job)


def _log(job: dict[str, Any], message: str) -> None:
    line = f"{time.strftime('%H:%M:%S')}  {message}"
    with _jobs_lock:
        job["log"].append(line)


def _set_dc(job: dict[str, Any], site: str, **fields: Any) -> None:
    match_session = fields.pop("match_session", None)
    match_demo_id = fields.pop("match_demo_id", None)
    with _jobs_lock:
        target = _find_dc(job, site, match_session, match_demo_id)
        if target is not None:
            new_phase = fields.get("phase")
            cur_phase = str(target.get("phase") or "")
            if (
                new_phase
                and cur_phase in _TERMINAL_DC_PHASES
                and str(new_phase) not in _TERMINAL_DC_PHASES
            ):
                fields = {
                    key: value
                    for key, value in fields.items()
                    if key not in {"phase", "message"}
                }
            target.update(fields)
            target["touchedAt"] = time.time()
    _persist_job(job)


def _update_dc_card(job: dict[str, Any], dc_card: dict[str, Any], **fields: Any) -> None:
    with _jobs_lock:
        if dc_card in (job.get("dcs") or []):
            dc_card.update(fields)
            dc_card["touchedAt"] = time.time()
    _persist_job(job)


def _demo_targets(demo_ids: DemoIds) -> list[tuple[str, str]]:
    targets = []
    mapping = demo_ids.model_dump()
    for site in SITES:
        raw = (mapping.get(site) or "").strip()
        if not raw:
            continue
        parsed_site, demo_id = parse_site_and_id(raw, site)
        site_code = parsed_site or site
        if demo_id:
            targets.append((site_code, demo_id))
    return targets


def _schedule_targets_from_conflict_body(body: ScheduleConflictCheckPayload) -> list[tuple[str, str]]:
    if body.items:
        targets: list[tuple[str, str]] = []
        for item in body.items:
            site = (item.site or "").strip().lower()
            demo_id = (item.content_id or "").strip()
            if site and demo_id:
                targets.append((site, demo_id))
        return targets
    return _demo_targets(body.demo_ids)


def _site_schedule_decision(
    payload: RunPayload,
    site: str,
) -> ScheduleSiteDecision | None:
    site_code = (site or "").strip().lower()
    for decision in payload.schedule_decisions:
        if (decision.site or "").strip().lower() == site_code:
            return decision
    return None


def _require_cai_cookie() -> str:
    return _cai_cookie()


def _require_camgr_cookie() -> str:
    cookie = _camgr_cookie()
    with _camgr_auth_lock:
        verified = bool(_camgr_auth.get("verified"))
        fresh = time.time() - float(_camgr_auth.get("verified_at") or 0) < CAMGR_VERIFY_TTL_SECONDS
    if not cookie or not verified or not fresh:
        # Repair first so the click works instead of failing once and asking for a retry.
        _camgr_auto_connect()
        cookie = _camgr_cookie()
    if not cookie:
        raise HTTPException(
            401,
            CAMGR_LOGIN_HINT,
        )
    return cookie


def _camgr_auto_connect() -> dict[str, Any]:
    """Re-probe CAMGR and, if that fails, pull a fresh Chrome cookie. Never raises."""
    with _camgr_open_lock:
        if _camgr_open.get("running"):
            return _camgr_public_status()
    if not host_resolves(CAMGR_HOST):
        _camgr_mark_unverified(off_network_message("CAMGR"))
        return _camgr_public_status()
    cookie = _camgr_cookie()
    probed = probe_camgr_login(cookie)
    if probed.get("loggedIn"):
        header = str(probed.get("cookie") or cookie or "").strip()
        _set_camgr_cookie(header, probed.get("message") or "", str(probed.get("user") or ""))
        return _camgr_public_status(
            {
                "loggedIn": True,
                "ok": True,
                "user": probed.get("user") or "",
                "message": probed.get("message") or "CAMGR session is active.",
            }
        )
    if _chrome_import_allowed("camgr"):
        imported, _import_message = import_camgr_cookies_from_chrome()
        if imported and imported != cookie:
            probed = probe_camgr_login(imported)
            if probed.get("loggedIn"):
                header = str(probed.get("cookie") or imported or "").strip()
                _set_camgr_cookie(header, probed.get("message") or "", str(probed.get("user") or ""))
                return _camgr_public_status(
                    {
                        "loggedIn": True,
                        "ok": True,
                        "user": probed.get("user") or "",
                        "message": probed.get("message") or "CAMGR session is active.",
                    }
                )
    # Keep whatever cookie we have. It may start working again, and the watcher
    # keeps checking Chrome, so signing in there is enough to reconnect.
    _camgr_mark_unverified(CAMGR_LOGIN_HINT)
    return _camgr_public_status({"loggedIn": False})


def _cai_auto_connect() -> dict[str, Any]:
    """Same idea for CAI, which usually only needs the Cisco network. Never raises."""
    if not host_resolves(CAI_HOST):
        return _cai_public_status(
            {"loggedIn": False, "ok": False, "message": off_network_message("CAI")}
        )
    cookie = _cai_cookie()
    probed = probe_cai_login(cookie)
    if not probed.get("loggedIn") and cookie:
        probed = probe_cai_login("")
        if probed.get("loggedIn"):
            cookie = ""
    if not probed.get("loggedIn") and _chrome_import_allowed("cai"):
        imported, _message = import_cai_cookies_from_chrome()
        if imported:
            probed = probe_cai_login(imported)
            if probed.get("loggedIn"):
                cookie = imported
    if not probed.get("loggedIn"):
        return _cai_public_status(probed)
    message = probed.get("message") or "CAI session is active."
    _mark_cai_reachable(message, cookie)
    return _cai_public_status({"loggedIn": True, "ok": True, "message": message})


def _auth_keepalive_loop() -> None:
    # Signed in, this just keeps the sessions warm. Signed out, it watches Chrome
    # so finishing Duo in a tab reconnects the tool on its own.
    checked_at = {"camgr": 0.0, "cai": 0.0}
    connected = {"camgr": False, "cai": False}
    probes = {"camgr": _camgr_auto_connect, "cai": _cai_auto_connect}
    while True:
        time.sleep(AUTH_WATCH_SECONDS)
        now = time.time()
        for kind, probe in probes.items():
            wait = AUTH_KEEPALIVE_SECONDS if connected[kind] else AUTH_WATCH_SECONDS
            if now - checked_at[kind] < wait:
                continue
            checked_at[kind] = now
            try:
                connected[kind] = bool(probe().get("loggedIn"))
            except Exception:
                connected[kind] = False


def _start_auth_keepalive() -> None:
    global _auth_keepalive_started
    with _auth_keepalive_lock:
        if _auth_keepalive_started:
            return
        _auth_keepalive_started = True
    threading.Thread(target=_auth_keepalive_loop, daemon=True).start()


def _camgr_open_worker() -> None:
    try:
        header, message = capture_camgr_session(CAMGR_BROWSER_PROFILE)
        if header:
            probed = probe_camgr_login(header)
            if probed.get("loggedIn"):
                _set_camgr_cookie(
                    header,
                    str(probed.get("message") or message or "CAMGR session is active."),
                    str(probed.get("user") or ""),
                )
                with _camgr_open_lock:
                    _camgr_open["error"] = ""
                return
            message = str(probed.get("message") or message or "")
        with _camgr_open_lock:
            _camgr_open["error"] = message or "Could not sign in to CAMGR."
    except Exception as exc:
        with _camgr_open_lock:
            _camgr_open["error"] = str(exc) or "Could not open CAMGR."
    finally:
        with _camgr_open_lock:
            _camgr_open["running"] = False


def _start_camgr_open() -> dict[str, Any]:
    with _camgr_open_lock:
        if _camgr_open.get("running"):
            return {
                "ok": True,
                "running": True,
                "message": "CAMGR window is already opening.",
            }
        _camgr_open["running"] = True
        _camgr_open["error"] = ""
    threading.Thread(target=_camgr_open_worker, daemon=True).start()
    return {
        "ok": True,
        "running": True,
        "message": "Opening a CAMGR window this tool controls. Sign in there if asked.",
    }


def _connect_camgr(cookie: str = "") -> dict[str, Any]:
    """Use a saved session, then the live CAMGR Chrome tab."""
    # Off the VPN the Chrome tab fails too, so say that instead of "open CAMGR".
    if not host_resolves(CAMGR_HOST):
        reason = off_network_message("CAMGR")
        _camgr_mark_unverified(reason)
        raise HTTPException(400, reason)
    _chrome_import_reset("camgr")
    header = (cookie or "").strip()
    probed = probe_camgr_login(header)
    tab_message = ""
    if not probed.get("loggedIn"):
        tab_header, tab_message = connect_camgr_via_chrome_tab()
        if tab_header:
            header = tab_header
            probed = probe_camgr_login(header)
    if not probed.get("loggedIn"):
        imported, _import_message = import_camgr_cookies_from_chrome()
        if imported:
            header = imported
            probed = probe_camgr_login(header)
    if not probed.get("loggedIn"):
        # The Chrome tab is the path that actually works, so its reason wins.
        reason = tab_message or probed.get("message") or CAMGR_LOGIN_HINT
        _camgr_mark_unverified(reason)
        raise HTTPException(400, reason)
    header = str(probed.get("cookie") or header or "").strip() or header
    user = str(probed.get("user") or "").strip()
    message = probed.get("message") or "CAMGR session is active."
    _set_camgr_cookie(header, message, user)
    return _camgr_public_status({"loggedIn": True, "ok": True, "user": user, "message": message})


def _auth_camgr_needed() -> dict[str, Any]:
    return {
        "ok": False,
        "loggedIn": False,
        "message": CAMGR_LOGIN_HINT,
    }


def _integrate_saved_demo(
    cookie: str,
    *,
    site: str,
    saved_id: str,
    dests: list[str],
    job: dict[str, Any] | None,
    wait_for_dests: bool = False,
) -> dict[str, Any]:
    dests = cai_integrate_dests(dests)
    if not site or not saved_id:
        return {"ok": False, "message": "Saved content ID is required."}
    if not dests:
        return {"ok": False, "message": "Select at least one destination DC to integrate."}
    page = fetch_demo_page(cookie, site, saved_id)
    if page.get("loggedIn") is False:
        return {
            "ok": False,
            "loggedIn": False,
            "message": page.get("message") or "CAI is not reachable. Click Connect to CAI.",
        }
    if not page.get("ok"):
        return {"ok": False, "message": page.get("message") or "Could not load CAI demo."}
    # Keep CAI's raw checkbox name per normalized code: EMEA's box is "lon".
    field_by_dc: dict[str, str] = {}
    for raw_dc in page.get("integrateDcs") or []:
        raw_name = str(raw_dc or "").strip()
        code = normalize_cai_dc(raw_name)
        if code and code not in field_by_dc:
            field_by_dc[code] = raw_name
    available = set(field_by_dc)
    missing = [dc for dc in dests if dc not in available]
    if wait_for_dests and missing:
        return {
            "ok": False,
            "waiting": True,
            "message": (
                "Transfer complete. Waiting for CAI dest DCs: "
                + ", ".join(cai_dc_label(dc) for dc in missing)
                + "."
            ),
            "available": sorted(available),
            "missing": missing,
        }
    chosen = [dc for dc in dests if dc in available] if available else dests
    if not chosen:
        return {
            "ok": False,
            "message": (
                f"{site.upper()} {saved_id}: no matching integrate DCs "
                f"(available: {', '.join(sorted(available)) or 'none'})."
            ),
        }
    cai_dc = str(page.get("caiDc") or "")
    result = submit_integrate(
        cookie,
        site,
        saved_id,
        dest_dcs=chosen,
        cai_dc=cai_dc,
        dest_fields=[field_by_dc.get(dc, dc) for dc in chosen],
    )
    if result.get("loggedIn") is False:
        return {
            "ok": False,
            "loggedIn": False,
            "message": result.get("message") or "CAI session expired.",
        }
    # A re-submit for one dest must not drop the dests already tracked on this
    # row, or their chips disappear and the refresher stops polling them.
    prev = _cai_integrate_map(job).get(_saved_id_key(site, saved_id)) or {}
    prev_dcs = cai_integrate_dests(list(prev.get("dcs") or []))
    prev_chips = [chip for chip in (prev.get("dcTasks") or []) if isinstance(chip, dict)]
    prev_by_dc = {str(chip.get("dc") or ""): chip for chip in prev_chips}
    merged = prev_dcs + [dc for dc in chosen if dc not in prev_dcs]
    overall = "submitted" if result.get("ok") else "error"
    chips = [
        prev_by_dc[chip["dc"]] if chip["dc"] not in chosen and chip["dc"] in prev_by_dc else chip
        for chip in cai_integrate_dc_chips(merged, [], overall=overall, previous=prev_chips)
    ]
    row = {
        "site": site,
        "savedId": saved_id,
        "dcs": merged,
        "status": overall,
        "message": result.get("message") or "",
        "caiUrl": result.get("caiUrl") or cai_demo_url(site, saved_id),
        "caiDc": result.get("caiDc") or cai_dc,
        "newId": str(prev.get("newId") or ""),
        "dcTasks": chips,
        "dcIdsChecked": False,
    }
    _upsert_cai_integrate(job, row)
    if job is not None:
        _log(
            job,
            f"{site.upper()}: CAI integration "
            + ("submitted" if result.get("ok") else "failed")
            + f" for {saved_id} → {', '.join(cai_dc_label(dc) for dc in chosen)}.",
        )
    return {
        "ok": bool(result.get("ok")),
        "loggedIn": True,
        "row": row,
        "chosen": chosen,
        "message": result.get("message") or "",
    }


def _auto_integrate_pending(item: dict[str, Any]) -> bool:
    if not item.get("autoIntegrate"):
        return False
    if str(item.get("status") or "").strip().lower() != "complete":
        return False
    flag = str(item.get("autoIntegrateStatus") or "").strip().lower()
    return flag in {"", "pending", "waiting"}


def _mark_auto_integrate_state(
    job: dict[str, Any] | None,
    item: dict[str, Any],
    *,
    status: str,
    message: str,
    dests: list[str],
    integrate_status: str = "",
) -> None:
    item["autoIntegrateStatus"] = status
    item["message"] = message
    _upsert_camgr_transfer(job, item)
    if integrate_status:
        _upsert_cai_integrate(
            job,
            {
                "site": str(item.get("site") or "").strip().lower(),
                "savedId": str(item.get("savedId") or "").strip(),
                "dcs": dests,
                "status": integrate_status,
                "message": message,
            },
        )


def _try_auto_integrate_transfer(job: dict[str, Any] | None, item: dict[str, Any]) -> dict[str, Any]:
    if not _auto_integrate_pending(item):
        return {"ok": False, "skipped": True}
    site = str(item.get("site") or "").strip().lower()
    saved_id = str(item.get("savedId") or "").strip()
    key = _saved_id_key(site, saved_id)
    dests = cai_integrate_dests(list(item.get("autoIntegrateDcs") or item.get("dcs") or []))
    if not dests:
        _mark_auto_integrate_state(
            job,
            item,
            status="skipped",
            message="Auto-integrate skipped: no CAI dest DCs in this transfer.",
            dests=[],
        )
        return {"ok": False, "skipped": True, "message": item["message"]}
    with _auto_integrate_lock:
        if key in _auto_integrate_busy:
            return {"ok": False, "skipped": True}
        _auto_integrate_busy.add(key)
    try:
        cookie = _require_cai_cookie()
        result = _integrate_saved_demo(
            cookie,
            site=site,
            saved_id=saved_id,
            dests=dests,
            job=job,
            wait_for_dests=True,
        )
        attempts = int(item.get("autoIntegrateAttempts") or 0) + 1
        item["autoIntegrateAttempts"] = attempts
        timed_out = attempts >= _AUTO_INTEGRATE_MAX_ATTEMPTS
        retryable = bool(result.get("waiting")) or result.get("loggedIn") is False
        if result.get("ok"):
            item["autoIntegrateStatus"] = "submitted"
            item["message"] = result.get("message") or "CAI integration submitted."
            row = result.get("row") or {}
            if item.get("autoBurnIn"):
                row.update(
                    {
                        "autoBurnIn": True,
                        "burnInDays": max(1, int(item.get("burnInDays") or 1)),
                        "burnInStatus": str(row.get("burnInStatus") or "pending"),
                        "burnInJobId": str(row.get("burnInJobId") or ""),
                    }
                )
                _upsert_cai_integrate(job, row)
            _upsert_camgr_transfer(job, item)
            return {"ok": True, "row": row, "message": item["message"]}
        if retryable and not timed_out:
            _mark_auto_integrate_state(
                job,
                item,
                status="waiting",
                message=str(result.get("message") or "Transfer complete. Waiting for CAI dest DCs."),
                dests=dests,
                integrate_status="waiting",
            )
            return {"ok": False, "waiting": True, "message": item["message"]}
        if retryable and timed_out:
            available = cai_integrate_dests(list(result.get("available") or []))
            if available:
                result = _integrate_saved_demo(
                    cookie,
                    site=site,
                    saved_id=saved_id,
                    dests=available,
                    job=job,
                    wait_for_dests=False,
                )
                if result.get("ok"):
                    item["autoIntegrateStatus"] = "submitted"
                    item["message"] = result.get("message") or "CAI integration submitted."
                    row = result.get("row") or {}
                    if item.get("autoBurnIn"):
                        row.update(
                            {
                                "autoBurnIn": True,
                                "burnInDays": max(1, int(item.get("burnInDays") or 1)),
                                "burnInStatus": str(row.get("burnInStatus") or "pending"),
                                "burnInJobId": str(row.get("burnInJobId") or ""),
                            }
                        )
                        _upsert_cai_integrate(job, row)
                    _upsert_camgr_transfer(job, item)
                    return {"ok": True, "row": row, "message": item["message"]}
            _mark_auto_integrate_state(
                job,
                item,
                status="error",
                message=str(
                    result.get("message")
                    or "Timed out waiting for CAI dest DCs. Use Load VMs and Submit integration."
                ),
                dests=dests,
                integrate_status="error",
            )
            return {"ok": False, "message": item["message"]}
        _mark_auto_integrate_state(
            job,
            item,
            status="error",
            message=str(result.get("message") or "CAI integration failed."),
            dests=dests,
            integrate_status="error",
        )
        return {"ok": False, "message": item["message"]}
    finally:
        with _auto_integrate_lock:
            _auto_integrate_busy.discard(key)


def _refresh_camgr_transfer_statuses(job: dict[str, Any] | None) -> dict[str, Any]:
    cookie = _require_camgr_cookie()
    items = []
    seen: set[str] = set()
    if job is not None:
        for item in job.get("camgrTransfers") or []:
            if not isinstance(item, dict):
                continue
            key = _saved_id_key(str(item.get("site") or ""), str(item.get("savedId") or ""))
            if key == ":" or key in seen:
                continue
            seen.add(key)
            items.append(item)
    for item in _managed_saved_state().get("transfers") or []:
        if not isinstance(item, dict):
            continue
        key = _saved_id_key(str(item.get("site") or ""), str(item.get("savedId") or ""))
        if key == ":" or key in seen:
            continue
        seen.add(key)
        items.append(item)
    changed = False
    auto_integrated: list[dict[str, Any]] = []
    for item in items:
        status = str(item.get("status") or "").strip().lower()
        if status in {"complete", "error"}:
            if _auto_integrate_pending(item):
                auto_hit = _try_auto_integrate_transfer(job, item)
                if auto_hit.get("ok"):
                    auto_integrated.append(auto_hit.get("row") or item)
                    changed = True
                elif auto_hit.get("waiting") or auto_hit.get("message"):
                    changed = True
            continue
        refreshed = refresh_camgr_job(
            cookie,
            guid=str(item.get("guid") or ""),
            demo_id=str(item.get("demoId") or item.get("savedId") or ""),
            source_dc=str(item.get("camgrDc") or ""),
            servers=list(item.get("servers") or []),
            dest_dcs=list(item.get("dcs") or []),
            owner=str(item.get("owner") or ""),
        )
        if refreshed.get("loggedIn") is False:
            _camgr_mark_unverified(refreshed.get("message") or "")
            return refreshed
        if not refreshed.get("ok"):
            return refreshed
        hit = refreshed.get("job") or {}
        if not refreshed.get("found") or not hit:
            continue
        new_status = str(hit.get("status") or "").strip().lower()
        new_raw = str(hit.get("statusRaw") or "").strip()
        new_progress = hit.get("progress")
        new_guid = str(hit.get("guid") or item.get("guid") or "")
        new_dc_status = hit.get("dcStatus") or []
        if (
            new_status != status
            or new_raw != str(item.get("statusRaw") or "")
            or new_progress != item.get("progress")
            or new_guid != str(item.get("guid") or "")
            or new_dc_status != (item.get("dcStatus") or [])
        ):
            item.update(
                {
                    "status": new_status,
                    "statusRaw": new_raw,
                    "progress": new_progress,
                    "guid": new_guid,
                    "sessionId": hit.get("sessionId") or item.get("sessionId") or 0,
                    "dcStatus": hit.get("dcStatus") or [],
                    "dcs": hit.get("dcs") or item.get("dcs") or [],
                    "message": f"{new_raw} {new_progress}%".strip()
                    if new_raw and new_status not in {"complete", "error"}
                    else (new_raw or item.get("message") or ""),
                }
            )
            _upsert_camgr_transfer(job, item)
            if job is not None and new_status in {"complete", "error"}:
                _log(
                    job,
                    f"{str(item.get('site') or '').upper()}: CAMGR transfer "
                    f"{new_raw or new_status} for {item.get('savedId')}.",
                )
            if new_status == "complete" and _auto_integrate_pending(item):
                auto_hit = _try_auto_integrate_transfer(job, item)
                if auto_hit.get("ok"):
                    auto_integrated.append(auto_hit.get("row") or item)
            changed = True
    if changed and job is not None:
        _persist_job(job)
    return {"ok": True, "loggedIn": True, "autoIntegrated": auto_integrated}


def _camgr_job_to_transfer_row(pub: dict[str, Any], *, site: str, saved_id: str) -> dict[str, Any]:
    status_raw = str(pub.get("statusRaw") or pub.get("status") or "").strip()
    progress = pub.get("progress") or 0
    return {
        "site": site,
        "savedId": saved_id,
        "demoId": str(pub.get("demoId") or saved_id),
        "camgrDc": str(pub.get("dc") or site_to_camgr_guid(site)),
        "servers": list(pub.get("servers") or []),
        "dcs": list(pub.get("dcs") or []),
        "guid": str(pub.get("guid") or ""),
        "status": str(pub.get("status") or status_raw).strip().lower(),
        "statusRaw": status_raw,
        "progress": progress,
        "sessionId": pub.get("sessionId") or 0,
        "owner": str(pub.get("owner") or ""),
        "dcStatus": list(pub.get("dcStatus") or []),
        "message": format_camgr_status(pub),
        "imported": True,
    }


def _camgr_job_dicts(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in (result.get("jobs") or []) if isinstance(item, dict)]


def _merge_camgr_jobs(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_guid: dict[str, dict[str, Any]] = {}
    extras: list[dict[str, Any]] = []
    for group in groups:
        for item in group:
            guid = str(item.get("guid") or "").strip()
            if guid:
                by_guid[guid] = item
            else:
                extras.append(item)
    return list(by_guid.values()) + extras


def _purge_foreign_camgr_imports(job: dict[str, Any] | None) -> None:
    me = _camgr_current_user().strip().lower()
    if not me:
        return
    hide: list[CaiDemoRef] = []

    def keep_transfer(row: dict[str, Any]) -> bool:
        owner = str(row.get("owner") or "").strip().lower()
        if owner and owner != me:
            if row.get("imported"):
                site = str(row.get("site") or "").strip()
                saved_id = str(row.get("savedId") or "").strip()
                if site and saved_id:
                    hide.append(CaiDemoRef(site=site, saved_id=saved_id))
            return False
        return True

    if job is not None:
        job["camgrTransfers"] = [
            row for row in (job.get("camgrTransfers") or []) if isinstance(row, dict) and keep_transfer(row)
        ]
    state = _managed_saved_state()
    state["transfers"] = [
        row for row in (state.get("transfers") or []) if isinstance(row, dict) and keep_transfer(row)
    ]
    _persist_managed_saved_state(state)
    if hide:
        _hide_saved_ids(job, hide)
        if job is not None:
            _persist_job(job)


def _import_in_progress_camgr_jobs(
    job: dict[str, Any] | None,
    items: list[CaiDemoRef] | None = None,
) -> dict[str, Any]:
    cookie = _require_camgr_cookie()
    me = _camgr_current_user()
    if not me:
        probed = probe_camgr_login(cookie)
        if probed.get("loggedIn") is False:
            _camgr_mark_unverified(probed.get("message") or "")
            return probed
        me = str(probed.get("user") or "").strip()
        if me:
            _set_camgr_cookie(cookie, probed.get("message") or "", me)
    _purge_foreign_camgr_imports(job)

    mine = list_camgr_jobs(cookie, only_not_status="", only_user=True)
    if mine.get("loggedIn") is False:
        _camgr_mark_unverified(mine.get("message") or "")
        return mine
    jobs = _camgr_job_dicts(mine) if mine.get("ok") else []
    from_only_user = bool(jobs)
    if not jobs:
        listed = list_camgr_jobs(cookie, only_not_status="", only_user=False)
        if listed.get("loggedIn") is False:
            _camgr_mark_unverified(listed.get("message") or "")
            return listed
        if not listed.get("ok"):
            return listed if not mine.get("ok") else {"ok": True, "loggedIn": True, "imported": [], "found": 0}
        jobs = _camgr_job_dicts(listed)

    me_l = me.strip().lower()

    def job_is_mine(raw: dict[str, Any]) -> bool:
        owner = str(raw.get("owner") or "").strip().lower()
        if me_l and owner:
            return owner == me_l
        return from_only_user

    jobs = [item for item in jobs if job_is_mine(item)]

    watch_statuses = set(IN_FLIGHT_STATUSES) | {"COMPLETE", "ERROR"}
    pubs: list[dict[str, Any]] = []
    for raw in jobs:
        pub = public_camgr_job(raw)
        status = str(pub.get("statusRaw") or "").strip().upper()
        if status not in watch_statuses:
            continue
        if not str(pub.get("demoId") or "").strip():
            continue
        if not job_is_mine(pub):
            continue
        pubs.append(pub)

    saved_rows = _saved_id_summary(job).get("rows") or []
    saved_by_key: dict[str, dict[str, Any]] = {}
    saved_ids: set[str] = set()
    for row in saved_rows:
        site = str(row.get("site") or "").strip().lower()
        saved_id = str(row.get("savedId") or "").strip()
        if not saved_id:
            continue
        saved_ids.add(saved_id)
        saved_by_key[_saved_id_key(site, saved_id)] = row
    for item in items or []:
        saved_id = str(item.saved_id or "").strip()
        if saved_id:
            saved_ids.add(saved_id)
            site = str(item.site or "").strip().lower()
            if site:
                saved_by_key.setdefault(_saved_id_key(site, saved_id), {"site": site, "savedId": saved_id})

    selected: list[tuple[str, str, dict[str, Any]]] = []
    for pub in pubs:
        demo_id = str(pub.get("demoId") or "").strip()
        site = camgr_guid_to_site(str(pub.get("dc") or ""))
        status = str(pub.get("statusRaw") or "").strip().upper()
        key = _saved_id_key(site, demo_id)
        in_list = key in saved_by_key or demo_id in saved_ids
        if status in IN_FLIGHT_STATUSES:
            selected.append((site, demo_id, pub))
        elif in_list:
            selected.append((site, demo_id, pub))

    imported: list[dict[str, Any]] = []
    seen: set[str] = set()
    for site, saved_id, pub in selected:
        if not site or not saved_id:
            continue
        key = _saved_id_key(site, saved_id)
        if key in seen:
            continue
        seen.add(key)
        existing = saved_by_key.get(key) or {}
        name = str(existing.get("name") or "").strip()
        if not name:
            demo = fetch_camgr_demo(cookie, site, saved_id)
            if demo.get("loggedIn") is False:
                _camgr_mark_unverified(demo.get("message") or "")
                return demo
            if demo.get("ok"):
                demo_owner = str(demo.get("owner") or "").strip().lower()
                if me_l and demo_owner and demo_owner != me_l:
                    continue
                name = str(demo.get("name") or "").strip()
        _upsert_managed_saved_rows(
            [
                {
                    "site": site,
                    "savedId": saved_id,
                    "name": name,
                }
            ]
        )
        row = _camgr_job_to_transfer_row(pub, site=site, saved_id=saved_id)
        _upsert_camgr_transfer(job, row)
        imported.append(row)
        if job is not None:
            _log(
                job,
                f"{site.upper()}: CAMGR {row.get('statusRaw') or row.get('status')} "
                f"for {saved_id}"
                + (f" ({row.get('progress')}%)" if row.get("progress") and str(row.get("statusRaw") or "").upper() not in {"COMPLETE", "ERROR"} else "")
                + ".",
            )

    if job is not None and imported:
        _persist_job(job)
    return {
        "ok": True,
        "loggedIn": True,
        "imported": imported,
        "found": len(pubs),
    }


def _upsert_cai_replace(job: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    site = str(item.get("site") or "").strip().lower()
    saved_id = str(item.get("savedId") or "").strip()
    rows = list(job.get("caiReplaces") or [])
    updated = False
    for row in rows:
        if str(row.get("site") or "").lower() == site and str(row.get("savedId") or "") == saved_id:
            row.update(item)
            updated = True
            break
    if not updated:
        rows.append(item)
    job["caiReplaces"] = rows
    for dc in job.get("dcs") or []:
        if str(dc.get("site") or "").lower() != site:
            continue
        if str(dc.get("savedId") or "").strip() != saved_id:
            continue
        dc["caiReplaceStatus"] = item.get("status") or ""
        if item.get("newId"):
            dc["caiReplaceNewId"] = item.get("newId")
    return item


def _refresh_cai_replace_statuses(job: dict[str, Any]) -> dict[str, Any]:
    cookie = _require_cai_cookie()
    listed = list_cai_tasks(cookie)
    if not listed.get("ok"):
        if listed.get("loggedIn") is False:
            _set_cai_cookie("", "CAI session expired.")
        return listed
    tasks = listed.get("tasks") or []
    changed = False
    for item in list(job.get("caiReplaces") or []):
        if str(item.get("status") or "") in {"completed", "error"}:
            continue
        vms = item.get("vms") or []
        # CAI queues one task per VM, so each VM carries its own status.
        vm_chips = cai_replace_vm_chips(
            vms,
            tasks,
            saved_id=str(item.get("savedId") or ""),
            target_id=str(item.get("targetId") or ""),
            overall=str(item.get("status") or ""),
            previous=list(item.get("vmTasks") or []),
        )
        if vm_chips != list(item.get("vmTasks") or []):
            item["vmTasks"] = vm_chips
            changed = True
        else:
            item["vmTasks"] = vm_chips
        statuses = [str(chip.get("status") or "") for chip in vm_chips]
        new_ids = [str(chip.get("newId") or "") for chip in vm_chips if str(chip.get("newId") or "").strip()]
        done_count = sum(1 for s in statuses if s == "completed")
        failed = [str(chip.get("vm") or "") for chip in vm_chips if str(chip.get("status") or "") == "error"]
        progress = f"{done_count} of {len(statuses)} VM(s) done" if statuses else ""
        if statuses and all(s == "completed" for s in statuses):
            item["status"] = "completed"
            item["message"] = f"Replacement completed for all {len(statuses)} VM(s)."
            if new_ids:
                item["newId"] = new_ids[0]
            _log(
                job,
                f"{str(item.get('site') or '').upper()}: CAI replacement completed for "
                f"{item.get('savedId')} ({', '.join(vms)}).",
            )
            changed = True
        elif failed:
            item["status"] = "error"
            item["message"] = (
                f"CAI reported an error for {', '.join(failed)}. {progress}."
                if progress
                else f"CAI reported an error for {', '.join(failed)}."
            )
            changed = True
        elif any(s == "processing" for s in statuses):
            item["status"] = "processing"
            item["message"] = f"CAI is processing the replacement — {progress}."
        elif statuses:
            if item.get("status") not in {"submitted", "queuing", "processing"}:
                item["status"] = "queuing"
            item["message"] = f"Queued in CAI — {progress}."
        _upsert_cai_replace(job, item)
    if changed:
        _persist_job(job)
    return {"ok": True, "loggedIn": True, "tasks": tasks}


def _upsert_cai_integrate(job: dict[str, Any] | None, item: dict[str, Any]) -> dict[str, Any]:
    site = str(item.get("site") or "").strip().lower()
    saved_id = str(item.get("savedId") or "").strip()
    key = _saved_id_key(site, saved_id)
    if job is not None:
        rows = list(job.get("caiIntegrates") or [])
        updated = False
        for row in rows:
            if _saved_id_key(str(row.get("site") or ""), str(row.get("savedId") or "")) == key:
                row.update(item)
                updated = True
                break
        if not updated:
            rows.append(item)
        job["caiIntegrates"] = rows
    state = _managed_saved_state()
    rows = [row for row in (state.get("integrates") or []) if isinstance(row, dict)]
    updated = False
    for row in rows:
        if _saved_id_key(str(row.get("site") or ""), str(row.get("savedId") or "")) == key:
            row.update(item)
            updated = True
            break
    if not updated:
        rows.append(item)
    state["integrates"] = rows
    _persist_managed_saved_state(state)
    return item


def _chip_needs_tbv3_link(chip: dict[str, Any]) -> bool:
    if not str(chip.get("newId") or "").strip():
        return False
    href = str(chip.get("href") or "")
    if href.startswith(TBV3_UI) and "/edit/" in href:
        return False
    return not bool(chip.get("hrefChecked"))


def _attach_tbv3_chip_links(chips: list[dict[str, Any]]) -> bool:
    """Resolve TBv3 /edit/{uid} URLs for Integrate dest custom-content IDs."""
    changed = False
    token = effective_dcloud_token()
    for chip in chips:
        if not isinstance(chip, dict):
            continue
        href = str(chip.get("href") or "")
        if href and TBV3_UI not in href:
            chip["href"] = ""
            href = ""
            changed = True
        if not _chip_needs_tbv3_link(chip):
            continue
        new_id = str(chip.get("newId") or "").strip()
        uid = str(chip.get("topologyUid") or "").strip()
        if uid and not uid.isdigit() and not href:
            chip["href"] = tbv3_edit_url(uid)
            chip["hrefChecked"] = True
            if "Topology Builder v3" not in str(chip.get("tip") or ""):
                chip["tip"] = f"{chip.get('tip') or chip.get('label')} · Topology Builder v3"
            changed = True
            continue
        if not token:
            continue
        site = cai_dc_to_dcloud_site(str(chip.get("dc") or ""))
        if not site:
            chip["hrefChecked"] = True
            changed = True
            continue
        content = fetch_content(token, site, new_id)
        if not content:
            chip["hrefChecked"] = True
            changed = True
            continue
        uid = extract_content_topology_uid(content, site)
        url = tbv3_edit_url(uid)
        chip["hrefChecked"] = True
        if url:
            chip["topologyUid"] = uid
            chip["href"] = url
            if "Topology Builder v3" not in str(chip.get("tip") or ""):
                chip["tip"] = f"{chip.get('tip') or chip.get('label')} · Topology Builder v3"
        changed = True
    return changed


def _new_burn_in_job(token: str) -> dict[str, Any]:
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "phase": "scheduling",
        "createdAt": time.time(),
        "log": [],
        "error": "",
        "stop": threading.Event(),
        "auth_resume": threading.Event(),
        "auth_lock": threading.Lock(),
        "auth_needed": False,
        "auth_message": "",
        "token": token,
        "token_source": "browser",
        "token_at": time.time(),
        "selected_vms": [],
        "contentExport": False,
        "dcs": [],
    }
    _copy_session_to_job(job)
    with _jobs_lock:
        _jobs[job_id] = job
    _log(job, f"Job {job_id}: scheduling post-integration burn-in sessions.")
    return job


def _queue_integration_burn_in(
    item: dict[str, Any],
    job: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, int]:
    if not item.get("autoBurnIn"):
        return job, 0
    burn_status = str(item.get("burnInStatus") or "pending").strip().lower()
    if burn_status not in {"", "pending", "waiting_id", "waiting_auth"}:
        return job, 0
    if str(item.get("status") or "").strip().lower() != "completed":
        return job, 0

    chips = [chip for chip in (item.get("dcTasks") or []) if isinstance(chip, dict)]
    completed = [
        chip for chip in chips if str(chip.get("status") or "").strip().lower() == "completed"
    ]
    targets: list[tuple[str, str]] = []
    for chip in completed:
        site = cai_dc_to_dcloud_site(str(chip.get("dc") or ""))
        demo_id = str(chip.get("newId") or "").strip()
        if site and demo_id and (site, demo_id) not in targets:
            targets.append((site, demo_id))
    expected = len(cai_integrate_dests(list(item.get("dcs") or [])))
    if not targets or (expected and len(targets) < expected):
        item["burnInStatus"] = "error" if item.get("dcIdsChecked") else "waiting_id"
        item["burnInMessage"] = (
            "Could not schedule burn-in because an integrated demo ID is missing."
            if item["burnInStatus"] == "error"
            else "Integration completed. Waiting for the new demo IDs before scheduling burn-in."
        )
        _upsert_cai_integrate(None, item)
        return job, 0

    key = _saved_id_key(str(item.get("site") or ""), str(item.get("savedId") or ""))
    with _burn_in_lock:
        if key in _burn_in_busy:
            return job, 0
        _burn_in_busy.add(key)
    try:
        token = _cached_user_access_token()
        if not token:
            item["burnInStatus"] = "waiting_auth"
            item["burnInMessage"] = "Burn-in is waiting for a dCloud login."
            _upsert_cai_integrate(None, item)
            return job, 0
        if job is None:
            job = _new_burn_in_job(token)
        existing = {
            (str(dc.get("site") or "").lower(), str(dc.get("demoId") or "").strip())
            for dc in (job.get("dcs") or [])
        }
        fresh = [target for target in targets if target not in existing]
        added = _append_demo_schedule_to_job(job, fresh, content_export=False)
        days = max(1, int(item.get("burnInDays") or 1))
        item.update(
            {
                "burnInStatus": "queued",
                "burnInDays": days,
                "burnInJobId": job["id"],
                "burnInTargets": [
                    {"site": site, "demoId": demo_id} for site, demo_id in targets
                ],
                "burnInMessage": (
                    f"Queued {len(targets)} integrated demo(s) for a {days}-day burn-in."
                ),
            }
        )
        _upsert_cai_integrate(None, item)
        _log(
            job,
            f"Queued {len(targets)} integrated demo(s) for a {days}-day burn-in "
            f"({', '.join(f'{site.upper()} {demo_id}' for site, demo_id in targets)}).",
        )
        _persist_job(job)
        return job, added
    finally:
        with _burn_in_lock:
            _burn_in_busy.discard(key)


def _refresh_cai_integrate_statuses(job: dict[str, Any] | None) -> dict[str, Any]:
    cookie = _require_cai_cookie()
    listed = list_cai_tasks(cookie)
    if not listed.get("ok"):
        if listed.get("loggedIn") is False:
            _set_cai_cookie("", "CAI session expired.")
        return listed
    tasks = listed.get("tasks") or []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    if job is not None:
        for item in job.get("caiIntegrates") or []:
            if not isinstance(item, dict):
                continue
            key = _saved_id_key(str(item.get("site") or ""), str(item.get("savedId") or ""))
            if key == ":" or key in seen:
                continue
            seen.add(key)
            items.append(item)
    for item in _managed_saved_state().get("integrates") or []:
        if not isinstance(item, dict):
            continue
        key = _saved_id_key(str(item.get("site") or ""), str(item.get("savedId") or ""))
        if key == ":" or key in seen:
            continue
        seen.add(key)
        items.append(item)
    changed = False
    for item in items:
        dests = cai_integrate_dests(list(item.get("dcs") or []))
        dc_tasks = list(item.get("dcTasks") or [])
        dest_pending = [
            chip
            for chip in dc_tasks
            if str(chip.get("status") or chip.get("phase") or "")
            not in {"completed", "error"}
        ]
        overall = str(item.get("status") or "")
        missing_ids = (not dc_tasks) or any(
            not str(chip.get("newId") or "").strip() for chip in dc_tasks
        )
        settled = (not dest_pending) and overall in {"completed", "error"}
        if overall == "waiting":
            continue
        if settled and (not missing_ids or item.get("dcIdsChecked")):
            if _attach_tbv3_chip_links(dc_tasks):
                item["dcTasks"] = dc_tasks
                _upsert_cai_integrate(job, item)
                changed = True
            continue
        saved_id_text = str(item.get("savedId") or "").strip()
        hits = match_integrate_task(tasks, saved_id=saved_id_text)
        # Pick up dests CAI knows about that this row doesn't (submitted straight
        # in CAI, or lost from an older row), so their chips come back.
        for task in hits:
            if str(task.get("demo") or "").strip() != saved_id_text:
                continue
            _source, dest = parse_cai_task_dc(str(task.get("dc") or ""))
            if dest and dest not in dests:
                dests.append(dest)
                item["dcs"] = dests
                changed = True
        chips = cai_integrate_dc_chips(dests, hits, overall=overall, previous=dc_tasks)
        if chips:
            item["dcTasks"] = chips
            item["newIds"] = {
                str(chip.get("dc") or ""): str(chip.get("newId") or "")
                for chip in chips
                if str(chip.get("newId") or "").strip()
            }
            if item["newIds"]:
                item["newId"] = ", ".join(
                    f"{cai_dc_label(dc)} {nid}" for dc, nid in item["newIds"].items()
                )
            _attach_tbv3_chip_links(chips)
        if not hits:
            if overall in {"completed", "error"}:
                item["dcIdsChecked"] = True
                _upsert_cai_integrate(job, item)
                changed = True
                continue
            if overall not in {"submitted", "queuing", "processing"}:
                item["status"] = "queuing"
                item["message"] = "Queued in CAI."
                _upsert_cai_integrate(job, item)
                changed = True
            elif chips:
                _upsert_cai_integrate(job, item)
                changed = True
            continue
        statuses = [str(chip.get("status") or "") for chip in chips] or [
            normalize_task_status(str(hit.get("status") or "")) for hit in hits
        ]
        if statuses and all(s == "completed" for s in statuses):
            was_done = overall == "completed"
            item["status"] = "completed"
            item["message"] = "Integration completed."
            item["dcIdsChecked"] = bool(chips) and all(
                str(chip.get("newId") or "").strip() for chip in chips
            )
            if not was_done and job is not None:
                _log(
                    job,
                    f"{str(item.get('site') or '').upper()}: CAI integration completed for "
                    f"{item.get('savedId')}"
                    + (f" · {item.get('newId')}" if item.get("newId") else "")
                    + ".",
                )
            changed = True
        elif any(s == "error" for s in statuses):
            item["status"] = "error"
            item["message"] = "CAI reported an error for this integration."
            item["dcIdsChecked"] = True
            changed = True
        elif any(s == "processing" for s in statuses):
            if overall != "processing" or chips:
                item["status"] = "processing"
                item["message"] = "CAI is processing the integration."
                changed = True
        elif statuses:
            if overall not in {"submitted", "queuing", "processing"} or chips:
                item["status"] = "queuing"
                item["message"] = "Queued in CAI."
                changed = True
        _upsert_cai_integrate(job, item)
    if changed and job is not None:
        _persist_job(job)

    burn_jobs: dict[int, dict[str, Any]] = {}
    burn_added = 0
    for item in items:
        days = max(1, int(item.get("burnInDays") or 1))
        burn_job, added = _queue_integration_burn_in(item, burn_jobs.get(days))
        if burn_job is not None:
            burn_jobs[days] = burn_job
        burn_added += added
    if job is not None and burn_added:
        _persist_job(job)
    for days, burn_job in burn_jobs.items():
        if not burn_job.get("dcs"):
            continue
        payload = RunPayload(
            dcloud_token_source="browser",
            demo_ids=DemoIds(),
            selected_vms=[],
            days=days,
            content_export=False,
            auto_next_available=True,
            skip_power_on=True,
        )
        threading.Thread(target=_run_job, args=(burn_job, payload), daemon=True).start()
    primary_burn_job = next(reversed(burn_jobs.values()), None)
    return {
        "ok": True,
        "loggedIn": True,
        "tasks": tasks,
        "burnInJob": primary_burn_job,
        "burnInJobs": list(burn_jobs.values()),
        "burnInScheduled": burn_added,
    }


_load_persisted_session()
_load_cai_session()
_load_camgr_session()
set_camgr_cookie_sink(_camgr_cookie_rotated)
_start_auth_keepalive()


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(
        (STATIC_DIR / "index.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/version")
def api_version() -> dict[str, str]:
    return {"version": APP_VERSION}


@app.get("/api/changelog")
def api_changelog() -> dict[str, str]:
    path = APP_DIR / "CHANGELOG.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    return {"version": APP_VERSION, "text": text}


_update_check_lock = threading.Lock()


def _apply_github_update() -> None:
    """Apply after the HTTP response; uvicorn reloads when the files change."""
    time.sleep(0.8)
    try:
        env = dict(os.environ)
        env.pop("DCLOUD_SKIP_UPDATE", None)
        subprocess.run(
            [sys.executable, str(APP_DIR / "update_from_github.py")],
            cwd=str(APP_DIR),
            env=env,
            timeout=180,
            check=False,
        )
        # The updater normally replaces app.py, which reload mode sees. Touching
        # it also covers a release where only VERSION or static files changed.
        os.utime(APP_DIR / "app.py", None)
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        if _update_check_lock.locked():
            _update_check_lock.release()


@app.post("/api/update/check")
def api_update_check() -> dict[str, Any]:
    """Check public GitHub and apply a newer VERSION without a manual restart."""
    if not _update_check_lock.acquire(blocking=False):
        return {
            "ok": True,
            "updating": True,
            "version": APP_VERSION,
            "message": "An update check is already running.",
        }
    try:
        from update_from_github import fetch_public_version, is_newer, load_config

        config = load_config()
        repo = str(config.get("repo") or "").strip()
        branch = str(config.get("branch") or "main").strip() or "main"
        if not repo:
            _update_check_lock.release()
            raise HTTPException(400, "GitHub updates are not configured.")
        remote_version = fetch_public_version(repo, branch)
        if not remote_version:
            _update_check_lock.release()
            raise HTTPException(503, "Could not reach GitHub. Try again later.")
        if not is_newer(remote_version, APP_VERSION):
            _update_check_lock.release()
            return {
                "ok": True,
                "updating": False,
                "version": APP_VERSION,
                "message": f"Version {APP_VERSION} is already current.",
            }
        threading.Thread(target=_apply_github_update, daemon=True).start()
        return {
            "ok": True,
            "updating": True,
            "version": remote_version,
            "message": f"Updating to Version {remote_version}. The page will reload automatically.",
        }
    except HTTPException:
        raise
    except Exception as exc:
        if _update_check_lock.locked():
            _update_check_lock.release()
        raise HTTPException(500, f"Could not check for updates: {exc}")


@app.get("/api/auth/status")
def api_auth_status() -> dict[str, Any]:
    dcloud = dcloud_auth_status(ENV_FILE, ENV_EXAMPLE_FILE)
    _maybe_backfill_refresh_from_chrome()
    _ensure_user_access_token()
    with _user_auth_lock:
        expires_at = float(_user_auth.get("expires_at") or 0)
        has_refresh = bool(_user_auth.get("refresh_token"))
        access_token = (_user_auth.get("access_token") or "").strip()
        logged_in = bool(access_token) and (
            not expires_at or time.time() < expires_at - 30
        )
    return {
        "dcloudOauthConfigured": dcloud["oauth_configured"],
        "dcloudTokenConfigured": bool(effective_dcloud_token()),
        "hasUsername": dcloud["has_username"],
        "hasPassword": dcloud["has_password"],
        "hasBasicToken": dcloud["has_basic_token"],
        "envExists": dcloud["env_exists"],
        "envAuthAllowed": _env_auth_allowed(),
        "userTokensOnly": not _env_auth_allowed(),
        "sessionLoggedIn": logged_in,
        "sessionHasRefresh": has_refresh,
        "sessionExpiresAt": int(expires_at) if expires_at else 0,
        "accessToken": access_token if logged_in else "",
        "version": APP_VERSION,
        "cai": _cai_public_status(),
        "camgr": _camgr_public_status(),
    }


@app.get("/api/auth/login/url")
def api_login_url(site: str = "rtp") -> dict[str, str]:
    mark_login_exchange_started()
    url, state = build_dcloud_login_url(site)
    return {"url": url, "state": state, "site": (site or "rtp").strip().lower()}


@app.post("/api/auth/login/exchange")
async def api_login_exchange(site: str = "rtp") -> dict[str, Any]:
    """Fast SSO completion: exchange /authenticate?code= via ui-tokens (includes refreshToken)."""
    site_code = (site or "rtp").strip().lower()
    token, refresh, site_out, message = await run_in_threadpool(
        partial(try_import_dcloud_session, oauth_only=True, site_hint=site_code),
    )
    if token:
        _apply_user_session(token, refresh, site_out or site_code, "browser")
    return {
        "ok": bool(token),
        "token": token,
        "message": message or ("Waiting for SSO sign-in…" if not token else "Logged in."),
        "hasRefresh": bool(refresh),
        "site": site_out or site_code,
    }


@app.post("/api/auth/session/clear")
def api_clear_session() -> dict[str, Any]:
    """Drop in-memory access/refresh tokens (for testing a fresh login flow)."""
    with _user_auth_lock:
        _user_auth.update(
            {
                "access_token": "",
                "refresh_token": "",
                "site": "rtp",
                "expires_at": 0.0,
                "source": "",
                "has_refresh": False,
            }
        )
    _persist_user_session()
    clear_login_scan_state()
    return {"ok": True, "message": "Cleared in-memory dCloud session."}


@app.post("/api/auth/refresh")
async def api_refresh_session() -> dict[str, Any]:
    with _user_auth_lock:
        refresh = (_user_auth.get("refresh_token") or "").strip()
        site = (_user_auth.get("site") or "rtp").strip().lower()
    if not refresh:
        raise HTTPException(
            400,
            "No refresh token in memory — use Log in to dCloud or Import from browser first.",
        )
    access, _new_refresh, expires_at, err = _refresh_session_token(refresh, site)
    if err or not access:
        raise HTTPException(400, err or "Could not refresh dCloud token.")
    return {
        "ok": True,
        "token": access,
        "expiresAt": int(expires_at),
        "message": "Refreshed dCloud access token.",
    }


@app.post("/api/dcloud/token/import-from-browser")
async def api_import_dcloud_token(
    dry_run: bool = False,
    full_scan: bool = True,
    storage_only: bool = False,
) -> dict[str, Any]:
    try:
        token, refresh, site, message = await run_in_threadpool(
            partial(
                try_import_dcloud_session,
                full_scan=full_scan,
                storage_only=storage_only,
            ),
        )
        if token and not dry_run:
            _apply_user_session(token, refresh, site, "browser")
            if not refresh:
                _maybe_backfill_refresh_from_chrome()
        return {
            "ok": bool(token),
            "token": token,
            "message": message,
            "hasRefresh": bool(refresh),
            "site": site or "",
        }
    except Exception as exc:
        raise HTTPException(
            400,
            f"Could not import dCloud token from browser: {exc}",
        ) from exc


@app.post("/api/dcloud/token/validate")
def api_validate_dcloud_token(body: dict[str, str]) -> dict[str, Any]:
    token = effective_dcloud_token(body.get("token"))
    if not token:
        raise HTTPException(400, "Token is required.")
    ok, message = validate_dcloud_token(token)
    return {"ok": ok, "message": message}


@app.post("/api/vms")
def api_load_vms(body: LoadVmsPayload) -> dict[str, Any]:
    token = _resolve_token(body)
    source = (body.source or "session").strip().lower()
    if source not in {"session", "content"}:
        raise HTTPException(400, "source must be session or content.")

    if source == "content":
        site = (body.site or "").strip().lower()
        content_id = (body.content_id or body.session_id or "").strip()
        if site not in SITES:
            site, content_id = parse_site_and_id(content_id, body.site)
        if not site or not content_id:
            raise HTTPException(400, "Provide a datacenter and a saved content ID.")
        vms, details, err = list_content_vms(token, site, content_id)
        if err:
            raise HTTPException(400, err)
        name = str(details.get("name") or "").strip()
        demo_id = str(details.get("contentId") or details.get("demoId") or "").strip()
        if not vms:
            raise HTTPException(
                400,
                f"Saved content {content_id} in {site.upper()} has no VMs in its topology.",
            )
        if body.skip_catalog_lookup or not name:
            lookups: dict[str, dict[str, Any]] = {}
            if demo_id:
                lookups[site] = {
                    "site": site,
                    "id": demo_id,
                    "source": "content",
                    "note": "from this saved content",
                }
            if body.skip_catalog_lookup:
                for other in SITES:
                    if other == site:
                        continue
                    lookups[other] = {
                        "site": other,
                        "id": "",
                        "source": "skipped",
                        "note": "catalog search skipped",
                    }
        else:
            lookups = lookup_demo_ids_across_sites(
                token,
                name,
                source_site=site,
                source_demo_id=demo_id,
            )
        return {
            "ok": True,
            "source": "content",
            "site": site.upper(),
            "contentId": demo_id,
            "topologyUid": details.get("topologyUid") or "",
            "name": name,
            "status": details.get("status") or "",
            "demoId": str(details.get("demoId") or "").strip(),
            "viewUrl": details.get("viewUrl") or "",
            "siteMismatch": details.get("siteMismatch") or "",
            "lookups": lookups,
            "vms": vms,
            "note": "Power and guest tools are only available for active sessions.",
        }

    site, session_id = parse_site_and_id(body.session_id, body.site)
    if not site or not session_id:
        raise HTTPException(
            400,
            "Provide a datacenter and an active session ID.",
        )
    vms, details, err = list_session_vms(token, site, session_id)
    if err:
        raise HTTPException(400, err)
    vms, power_err = apply_tbv3_power_states(token, site, session_id, vms, details)
    if is_auth_error(power_err):
        raise HTTPException(401, power_err or "dCloud token was rejected.")
    name = str(details.get("name") or "").strip()
    demo_id = str(details.get("demoId") or details.get("parentId") or "").strip()
    if not vms:
        hint = (
            f"Session {session_id} in {site.upper()} has no VMs. "
            "Use a running session ID from the dCloud dashboard (Find my sessions), "
            "not the content / demo ID."
        )
        if demo_id and demo_id == session_id:
            hint = (
                f"{session_id} is the content ID, not a session ID. "
                "Switch Step 2 to Saved content, or use a running session ID from Find my sessions."
            )
        raise HTTPException(400, hint)
    if body.skip_catalog_lookup or not name:
        lookups = {}
        if demo_id:
            lookups[site] = {
                "site": site,
                "id": demo_id,
                "source": "session",
                "note": "from this session",
            }
        if body.skip_catalog_lookup:
            for other in SITES:
                if other == site:
                    continue
                lookups[other] = {
                    "site": other,
                    "id": "",
                    "source": "skipped",
                    "note": "catalog search skipped",
                }
    else:
        lookups = lookup_demo_ids_across_sites(
            token,
            name,
            source_site=site,
            source_demo_id=demo_id,
        )
    return {
        "ok": True,
        "source": "session",
        "site": site.upper(),
        "sessionId": session_id,
        "name": name,
        "status": details.get("status") or "",
        "demoId": demo_id,
        "viewUrl": session_view_url(site, session_id, session=details),
        "lookups": lookups,
        "vms": vms,
    }


def _claim_dc_for_schedule(job: dict[str, Any], dc: dict[str, Any]) -> bool:
    """Ensure only one worker schedules a given DC card (prevents duplicate dCloud sessions)."""
    schedule_lock = job.setdefault("_schedule_lock", threading.Lock())
    with schedule_lock:
        if job["stop"].is_set() or not _dc_needs_schedule(dc):
            return False
        if dc.get("_schedule_claimed"):
            return False
        dc["_schedule_claimed"] = True
        return True


def _schedule_one_dc(
    job: dict[str, Any],
    dc: dict[str, Any],
    payload: RunPayload,
    *,
    progress: Callable[[str], None],
    current_token: Callable[[], str],
    recover: Callable[[str], tuple[str, str | None]],
) -> bool:
    if not _claim_dc_for_schedule(job, dc):
        return False
    site = dc["site"]
    demo_id = dc.get("demoId") or ""
    kind = "exported" if payload.content_export else "regular"
    decision = _site_schedule_decision(payload, site)
    if decision and decision.action == "skip":
        _update_dc_card(
            job,
            dc,
            phase="ended",
            message="Skipped — not scheduled at the selected time.",
        )
        return False
    schedule_start = payload.start_at
    schedule_stop = payload.stop_at
    auto_next = payload.auto_next_available
    if decision and decision.action == "schedule_next":
        schedule_start = decision.start_at or schedule_start
        schedule_stop = decision.stop_at or schedule_stop
    _update_dc_card(job, dc, phase="scheduling", message="Asking dCloud for a slot…")
    tok = current_token()
    result = schedule_exported_session(
        tok,
        site,
        demo_id,
        days=payload.days,
        start_at=schedule_start,
        stop_at=schedule_stop,
        content_export=payload.content_export,
        auto_next_available=auto_next,
        progress=progress,
    )
    if not result.get("ok") and is_auth_error(result.get("message")):
        tok, auth_err = recover(tok)
        if auth_err:
            _update_dc_card(job, dc, phase="error", message=auth_err)
            return False
        result = schedule_exported_session(
            tok,
            site,
            demo_id,
            days=payload.days,
            start_at=schedule_start,
            stop_at=schedule_stop,
            content_export=payload.content_export,
            auto_next_available=auto_next,
            progress=progress,
        )
    if not result.get("ok"):
        if result.get("conflict"):
            _update_dc_card(
                job,
                dc,
                phase="error",
                message=result.get("message") or "Selected time is not available.",
            )
            return False
        _update_dc_card(
            job,
            dc,
            phase="error",
            message=result.get("message") or "Schedule failed.",
        )
        return False
    sched_fields: dict[str, str] = {}
    if result.get("scheduleStart") and result.get("scheduleStop"):
        sched_fields = {
            "scheduleStart": str(result["scheduleStart"]),
            "scheduleStop": str(result["scheduleStop"]),
        }
    else:
        window = resolve_schedule_window(
            days=payload.days,
            start_at=payload.start_at,
            stop_at=payload.stop_at,
        )
        if isinstance(window, tuple):
            sched_fields = {
                "scheduleStart": _dcloud_timestamp(window[0]),
                "scheduleStop": _dcloud_timestamp(window[1]),
            }
    _update_dc_card(
        job,
        dc,
        phase="waiting",
        sessionId=result.get("sessionId") or "",
        pool=result.get("pool") or "",
        viewUrl=result.get("viewUrl") or "",
        contentExport=payload.content_export,
        message=result.get("message") or "Waiting for dCloud to start this session.",
        **sched_fields,
    )
    return True


def _parallel_schedule_pending(
    job: dict[str, Any],
    payload: RunPayload,
    *,
    progress: Callable[[str], None],
    current_token: Callable[[], str],
    recover: Callable[[str], tuple[str, str | None]],
) -> list[dict[str, Any]]:
    schedule_lock = job.setdefault("_schedule_lock", threading.Lock())
    pending: list[dict[str, Any]] = []
    deadline = time.time() + 600
    while True:
        with schedule_lock:
            if not job.get("_schedule_in_progress"):
                pending = [dc for dc in list(job.get("dcs") or []) if _dc_needs_schedule(dc)]
                if not pending:
                    return []
                job["_schedule_in_progress"] = True
                break
        if time.time() > deadline:
            progress("Timed out waiting for another schedule batch to finish.")
            return []
        time.sleep(0.25)
    scheduled: list[dict[str, Any]] = []
    scheduled_lock = threading.Lock()

    try:
        def _one(dc: dict[str, Any]) -> None:
            if _schedule_one_dc(
                job,
                dc,
                payload,
                progress=progress,
                current_token=current_token,
                recover=recover,
            ):
                with scheduled_lock:
                    scheduled.append(dc)

        workers = max(len(pending), 1)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_one, dc) for dc in pending]
            for future in as_completed(futures):
                future.result()
    finally:
        with schedule_lock:
            job["_schedule_in_progress"] = False
    return scheduled


def _watch_and_power_dc(
    job: dict[str, Any],
    payload: RunPayload,
    site: str,
    session_id: str,
    selected: list[dict[str, Any]],
    *,
    progress: Callable[[str], None],
    current_token: Callable[[], str],
    recover: Callable[[str], tuple[str, str | None]],
) -> None:
    if not session_id:
        return
    wait = wait_until_active(
        current_token(),
        site,
        session_id,
        timeout_seconds=payload.active_timeout_minutes * 60,
        progress=progress,
        should_stop=job["stop"].is_set,
        on_session=lambda details, dc_site=site, sid=session_id: _set_dc(
            job,
            dc_site,
            match_session=sid,
            viewUrl=session_view_url(dc_site, sid, session=details),
            canReset=bool(details.get("canReset")),
            **_dc_ids_from_session(details),
        ),
        # Keep the card in step with the log while dCloud builds the session.
        on_status=lambda status, details, dc_site=site, sid=session_id: _set_dc(
            job,
            dc_site,
            match_session=sid,
            status=status,
            message="Waiting for dCloud to bring this session up.",
        ),
        get_token=current_token,
        refresh_auth=recover,
    )
    if wait.get("saved"):
        _mark_dc_saved(
            job,
            site,
            details=wait.get("session") if isinstance(wait.get("session"), dict) else None,
            status=wait.get("status") or "",
            message=wait.get("message") or "Session already saved.",
            session_id=session_id,
        )
        return
    if not wait.get("ok"):
        details = wait.get("session") if isinstance(wait.get("session"), dict) else {}
        _set_dc(
            job,
            site,
            match_session=session_id,
            phase="error",
            status=wait.get("status") or "",
            canReset=bool(details.get("canReset")),
            message=wait.get("message") or "Failed.",
        )
        return
    tok = current_token()
    vms, _details, err = list_session_vms(tok, site, session_id)
    if is_auth_error(err):
        tok, auth_err = recover(tok)
        if auth_err:
            _set_dc(job, site, match_session=session_id, phase="error", message=auth_err)
            return
        vms, _details, err = list_session_vms(tok, site, session_id)
    if err:
        _set_dc(job, site, match_session=session_id, phase="error", message=err)
        return
    matched, missing = match_selected_vms(vms, selected)
    if missing:
        progress(f"{site.upper()}: could not match VMs by name: {', '.join(missing)}")
    if not matched:
        if selected:
            progress(f"{site.upper()}: no VM name matches in this session — skipping power on.")
        else:
            progress(f"{site.upper()}: no VMs were checked, so nothing is being powered on.")
        matched = []
    power_targets = [
        {
            "name": vm.get("name"),
            "displayName": vm.get("displayName") or vm.get("name"),
            "shortName": vm.get("shortName") or "",
            "mor": vm.get("mor"),
            "uid": vm.get("uid"),
        }
        for vm in matched
    ]
    _set_dc(
        job,
        site,
        match_session=session_id,
        phase="powering",
        status="Active",
        message="Matching VMs and powering on…",
        powerOnPending=bool(power_targets),
        powerOnTargets=power_targets,
    )
    tok = current_token()
    results = []
    if matched:
        results = power_on_vms(tok, site, session_id, matched, progress=progress)
        wait_for_power_state(
            tok,
            site,
            session_id,
            matched,
            want_on=True,
            timeout_seconds=600,
            progress=progress,
            should_stop=job["stop"].is_set,
            get_token=current_token,
            refresh_auth=recover,
        )
    live, live_details, _ = list_session_vms(current_token(), site, session_id)
    live, _power_err = apply_tbv3_power_states(
        current_token(), site, session_id, live, live_details
    )
    live = attach_vm_access_links(current_token(), site, session_id, live)
    live = tag_selected_vms(live, power_targets or selected)
    failed = [r for r in results if not r.get("ok")]
    # The demo powers its own VMs on, so report what is actually off rather than
    # what this tool was asked to start.
    powered_off = [vm for vm in (live or []) if not _vm_is_powered_on(vm)]
    message = _active_card_message(payload.content_export)
    if failed:
        message = "Power-on finished with errors: " + "; ".join(
            f"{r.get('name')}: {r.get('message')}" for r in failed
        )
    elif not matched and powered_off:
        message += " Expand Powered off below if you need another VM started."
    _set_dc(
        job,
        site,
        match_session=session_id,
        phase="ready",
        status="Active",
        message=message,
        vms=live,
        powerResults=results,
        powerOnTargets=power_targets,
        autoPowered=True,
        powerOnPending=False,
        name=str((live_details or {}).get("name") or "").strip(),
        viewUrl=wait.get("viewUrl")
        or session_view_url(site, session_id, session=live_details or wait.get("session")),
    )


def _spawn_watch_threads(
    job: dict[str, Any],
    payload: RunPayload,
    selected: list[dict[str, Any]],
    *,
    progress: Callable[[str], None],
    current_token: Callable[[], str],
    recover: Callable[[str], tuple[str, str | None]],
    dcs: list[dict[str, Any]] | None = None,
) -> list[threading.Thread]:
    threads: list[threading.Thread] = []
    for dc in dcs or list(job.get("dcs") or []):
        if dc.get("phase") in {"error", "saved", "ended", "ready"}:
            continue
        session_id = str(dc.get("sessionId") or "").strip()
        if not session_id:
            continue
        thread = threading.Thread(
            target=_watch_and_power_dc,
            args=(job, payload, dc["site"], session_id, selected),
            kwargs={
                "progress": progress,
                "current_token": current_token,
                "recover": recover,
            },
            daemon=True,
        )
        threads.append(thread)
        thread.start()
    return threads


def _new_demo_dc_card(site: str, demo_id: str, *, content_export: bool) -> dict[str, Any]:
    return {
        "site": site,
        "demoId": demo_id,
        "sessionId": "",
        "phase": "queued",
        "message": "Waiting to be scheduled.",
        "pool": "",
        "viewUrl": "",
        "status": "",
        "vms": [],
        "savedId": "",
        "contentExport": content_export,
    }


def _append_demo_schedule_to_job(
    job: dict[str, Any],
    targets: list[tuple[str, str]],
    *,
    content_export: bool,
) -> int:
    kind = "exported" if content_export else "regular"
    added = 0
    for site, demo_id in targets:
        job["dcs"].append(_new_demo_dc_card(site, demo_id, content_export=content_export))
        added += 1
    if added:
        _log(
            job,
            f"Queued {added} additional {kind} demo schedule(s) on this job "
            f"({', '.join(site.upper() for site, _ in targets)}).",
        )
        _sync_job_phase(job)
    return added


def _schedule_and_watch_new_dcs(job: dict[str, Any], payload: RunPayload) -> None:
    def progress(message: str) -> None:
        _log(job, message)

    try:
        if not job.get("token"):
            token = _resolve_token(payload, progress)
            job["token"] = token
            job["token_source"] = payload.dcloud_token_source
            job["token_at"] = time.time()
            _copy_session_to_job(job)
        job.setdefault("auth_lock", threading.Lock())
        job.setdefault("auth_resume", threading.Event())
        if payload.skip_power_on:
            # Scheduling with nothing checked, so do not fall back to the job's old picks.
            selected = []
        else:
            selected = (
                [vm.model_dump() for vm in payload.selected_vms]
                if payload.selected_vms
                else list(job.get("selected_vms") or [])
            )
        job["selected_vms"] = selected

        def current_token() -> str:
            return _job_token(job, progress)

        def recover(stale: str) -> tuple[str, str | None]:
            return _recover_auth(job, progress, stale)

        scheduled = _parallel_schedule_pending(
            job,
            payload,
            progress=progress,
            current_token=current_token,
            recover=recover,
        )
        if scheduled:
            progress(f"Scheduled {len(scheduled)} session(s) in parallel.")
        _spawn_watch_threads(
            job,
            payload,
            selected,
            progress=progress,
            current_token=current_token,
            recover=recover,
            dcs=scheduled,
        )
    except Exception as exc:
        _log(job, f"Schedule error: {exc}")
        _log(job, traceback.format_exc())


def _run_job(job: dict[str, Any], payload: RunPayload) -> None:
    def progress(message: str) -> None:
        _log(job, message)

    try:
        job["worker_alive"] = True
        token = _resolve_token(payload, progress)
        job["token"] = token
        job["token_source"] = payload.dcloud_token_source
        job["token_at"] = time.time()
        _copy_session_to_job(job)
        job.setdefault("auth_lock", threading.Lock())
        job.setdefault("auth_resume", threading.Event())
        if payload.skip_power_on or not payload.content_export:
            selected = []
        else:
            selected = [vm.model_dump() for vm in payload.selected_vms]
        job["selected_vms"] = selected
        job["phase"] = "scheduling"

        def current_token() -> str:
            return _job_token(job, progress)

        def recover(stale: str) -> tuple[str, str | None]:
            return _recover_auth(job, progress, stale)

        scheduled = _parallel_schedule_pending(
            job,
            payload,
            progress=progress,
            current_token=current_token,
            recover=recover,
        )
        if scheduled:
            progress(f"Scheduled {len(scheduled)} session(s) in parallel.")

        job["phase"] = "waiting_active"
        threads = _spawn_watch_threads(
            job,
            payload,
            selected,
            progress=progress,
            current_token=current_token,
            recover=recover,
        )
        for thread in threads:
            thread.join()

        if job["stop"].is_set():
            job["phase"] = "cancelled"
            progress("Stopped.")
            return

        phases = {dc.get("phase") for dc in job["dcs"]}
        if phases <= {"error", "ended"}:
            if phases == {"ended"}:
                job["phase"] = "ended"
                progress("No sessions were scheduled.")
            else:
                job["phase"] = "error"
                job["error"] = "All datacenters failed."
        elif "ready" in phases:
            job["phase"] = "ready_to_patch"
            progress("Sessions ready. Guest-shutdown & save when you are done.")
        elif "saved" in phases:
            job["phase"] = "complete"
            progress("Sessions already saved.")
        else:
            job["phase"] = "error"
            job["error"] = "No sessions reached the ready state."
    except HTTPException as exc:
        job["phase"] = "error"
        job["error"] = str(exc.detail)
        _log(job, str(exc.detail))
    except Exception as exc:
        job["phase"] = "error"
        job["error"] = str(exc)
        _log(job, f"Error: {exc}")
        _log(job, traceback.format_exc())
    finally:
        job["worker_alive"] = False


def _shutdown_one_dc(
    job: dict[str, Any],
    dc: dict[str, Any],
    payload: ShutdownPayload,
    *,
    progress: Callable[[str], None],
    current_token: Callable[[], str],
    recover: Callable[[str], tuple[str, str | None]],
    selected: list[dict[str, Any]],
) -> None:
    site = dc["site"]
    session_id = dc.get("sessionId") or ""
    if not session_id:
        return
    already_claimed = bool(dc.get("_save_claimed"))
    retry_save_only = bool(dc.get("_save_retry_only")) or _dc_save_retry_only(dc)
    if not already_claimed and not _claim_dc_for_save(job, dc):
        progress(
            f"{site.upper()}: skipping shutdown & save "
            f"(already saved, ended, or in progress — status {dc.get('phase')})."
        )
        return
    retry_save_only = bool(dc.get("_save_retry_only")) or retry_save_only
    results = dc.get("shutdownResults") or []
    if retry_save_only:
        progress(f"{site.upper()}: VMs already shut down; retrying save…")
    else:
        _set_dc(
            job,
            site,
            match_session=session_id,
            phase="shutting_down",
            message="Telling each guest OS to shut down…",
        )
        tok = current_token()
        vms, _, err = list_session_vms(tok, site, session_id)
        if is_auth_error(err):
            tok, auth_err = recover(tok)
            if auth_err:
                _release_save_claim(job, dc)
                _set_dc(job, site, match_session=session_id, phase="error", message=auth_err)
                return
            vms, _, err = list_session_vms(tok, site, session_id)
        if err:
            _release_save_claim(job, dc)
            _set_dc(job, site, match_session=session_id, phase="error", message=err)
            return
        dc_vms = dc.get("vms") or []
        preferred = [vm for vm in dc_vms if vm.get("selected") is True]
        if not preferred:
            preferred = selected or dc_vms
        matched, missing = match_selected_vms(vms, preferred)
        if missing:
            progress(f"{site.upper()}: shutdown match miss: {', '.join(missing)}")
        if not matched:
            matched = preferred
        tok = current_token()
        results = guest_shutdown_vms(tok, site, session_id, matched, progress=progress)
        progress(f"{site.upper()}: guest shutdown requests sent — starting save (dCloud handles shutdown on save).")
    _set_dc(
        job,
        site,
        match_session=session_id,
        phase="saving",
        message="Save submitted — waiting for dCloud.",
        shutdownResults=results,
    )
    saved = save_session(
        current_token(),
        site,
        session_id,
        save_url=payload.save_url,
        save_method=payload.save_method,
        source_demo_id=str(dc.get("demoId") or ""),
        name=_save_name_for_dc(payload, dc),
        description=_save_description_for_dc(payload, dc),
        progress=progress,
    )
    if saved.get("ok"):
        saved_id = saved.get("savedId") or ""
        # dCloud accepted the save but is still working, so keep the card up until it finishes.
        msg = "Saving in dCloud… the card clears when dCloud finishes."
        if saved_id:
            msg = f"Saved content ID {saved_id} — dCloud is still saving…"
        _set_dc(
            job,
            site,
            match_session=session_id,
            phase="saving",
            savePending=True,
            message=msg,
            savedId=saved_id,
            savedName=saved.get("savedName") or "",
            savedParentId=saved.get("parentId") or "",
            savedTopologyUid=saved.get("topologyVersionUid") or "",
            contentViewUrl=saved.get("contentViewUrl") or "",
            saveConfirmed=True,
        )
        _persist_saved_ids(job)
        if saved_id:
            progress(f"{site.upper()}: keep this ID for LC promotions / VM replacement: {site}{saved_id}")
        progress(f"{site.upper()}: save accepted — watching dCloud until the session finishes saving.")
    else:
        _release_save_claim(job, dc)
        _set_dc(
            job,
            site,
            match_session=session_id,
            phase="save_failed",
            message="Guest shutdown done, but save failed: "
            + (saved.get("message") or "unknown error"),
        )


def _shutdown_job(
    job: dict[str, Any],
    payload: ShutdownPayload,
    targets: list[dict[str, Any]] | None = None,
) -> None:
    def progress(message: str) -> None:
        _log(job, message)

    token = job.get("token") or _resolve_token(payload, progress)
    job["token"] = token
    job.setdefault("auth_lock", threading.Lock())
    job.setdefault("auth_resume", threading.Event())

    def current_token() -> str:
        return _job_token(job, progress)

    def recover(stale: str) -> tuple[str, str | None]:
        return _recover_auth(job, progress, stale)

    wanted = {site.strip().lower() for site in payload.sites if site.strip()}
    job["phase"] = "shutting_down"
    selected = job.get("selected_vms") or []

    if targets is None:
        targets = [
            dc
            for dc in _iter_save_candidates(job, payload)
            if _dc_owned_by_me(job, dc) is not False and _claim_dc_for_save(job, dc)
        ]
    if not targets:
        job["phase"] = "error"
        job["error"] = "No ready sessions matched for shutdown/save."
        _persist_job(job)
        return

    progress(
        f"Guest shutdown & save for {len(targets)} DC(s) in parallel "
        "(shutdown requests only — not waiting for VMs to power off)…"
    )
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = {
            pool.submit(
                _shutdown_one_dc,
                job,
                dc,
                payload,
                progress=progress,
                current_token=current_token,
                recover=recover,
                selected=selected,
            ): dc
            for dc in targets
        }
        for future in as_completed(futures):
            dc = futures[future]
            try:
                future.result()
            except Exception:
                progress(
                    f"{str(dc.get('site') or '').upper()}: shutdown/save crashed — "
                    f"{traceback.format_exc().splitlines()[-1]}"
                )
                _release_save_claim(job, dc)
                _set_dc(
                    job,
                    dc["site"],
                    match_session=str(dc.get("sessionId") or ""),
                    phase="error",
                    message="Shutdown/save thread failed unexpectedly.",
                )

    phases = {dc.get("phase") for dc in job["dcs"] if (not wanted or dc["site"] in wanted)}
    if "saving" in phases or "shutting_down" in phases:
        # Cards stay up showing progress until dCloud reports the save finished.
        job["phase"] = "saving"
        progress("Save requested — waiting for dCloud to finish saving.")
        _persist_job(job)
        _ensure_status_watch(job)
        return
    if phases <= {"saved"}:
        job["phase"] = "complete"
        progress("Done.")
    elif "saved" in phases:
        job["phase"] = "complete"
        progress("Finished with some errors — check each DC.")
    else:
        job["phase"] = "error"
        job["error"] = "Shutdown/save did not complete successfully."
    _persist_job(job)


def _end_job(job: dict[str, Any], payload: EndPayload) -> None:
    def progress(message: str) -> None:
        _log(job, message)

    token = job.get("token") or _resolve_token(payload, progress)
    job["token"] = token
    wanted = {site.strip().lower() for site in payload.sites if site.strip()}
    wanted_sid = str(payload.session_id or "").strip()
    session_pairs = _session_ref_pairs(payload.sessions)
    use_pairs = session_pairs if session_pairs else None
    job["phase"] = "ending"
    ended = 0
    attempted = 0
    for dc in job["dcs"]:
        site = dc["site"]
        session_id = dc.get("sessionId") or ""
        if not _dc_matches_action(
            dc,
            session_pairs=use_pairs,
            wanted_sites=wanted,
            wanted_sid=wanted_sid,
        ):
            continue
        if not session_id:
            continue
        # Monitoring cards are never part of a bulk end, however they were targeted.
        if dc.get("monitorOnly") and not payload.single_card:
            progress(f"{site.upper()}: skip end — card is in session monitoring.")
            continue
        if not payload.single_card and _dc_too_old_for_bulk(job, dc):
            progress(f"{site.upper()}: skip end — card is left over from an earlier run.")
            continue
        attempted += 1
        if dc.get("phase") == "ended":
            progress(f"{site.upper()}: skip end (already ended).")
            continue
        if dc.get("phase") == "saved" and dc.get("saveConfirmed"):
            progress(f"{site.upper()}: skip end (already saved).")
            continue
        if dc.get("phase") in {"saving", "shutting_down"} or dc.get("_save_claimed"):
            progress(f"{site.upper()}: skip end (save is already in progress).")
            continue
        _set_dc(job, site, match_session=session_id, phase="ending", message="Ending session (no save)…")
        result = end_session(token, site, session_id)
        if result.get("ok"):
            ended += 1
            _set_dc(
                job,
                site,
                match_session=session_id,
                phase="ended",
                endedWithoutSave=True,
                saveConfirmed=False,
                savedId="",
                savedName="",
                savedParentId="",
                contentViewUrl="",
                message=result.get("message") or "Session ended (not saved).",
            )
            progress(f"{site.upper()}: session {session_id} ended (not saved).")
        else:
            _set_dc(
                job,
                site,
                match_session=session_id,
                phase="error",
                message=result.get("message") or "End session failed.",
            )
            progress(f"{site.upper()}: end failed: {result.get('message')}")
    live = {
        dc.get("phase")
        for dc in job["dcs"]
        if dc.get("sessionId") and dc.get("phase") not in {"ended", "saved"}
    }
    if not attempted:
        job["phase"] = "error"
        job["error"] = "No session IDs to end. Restore the last job or attach running sessions first."
    elif not live:
        job["phase"] = "ended"
        progress(f"Ended {ended} session(s) without saving." if ended else "Nothing left to end.")
    elif ended:
        job["phase"] = "ready_to_patch"
        progress(f"Ended {ended} session(s). Other DCs are still running.")
    else:
        job["phase"] = "error"
        job["error"] = "No sessions were ended."
    _persist_job(job)


def _reset_job(job: dict[str, Any], payload: "ResetPayload") -> None:
    """dCloud's dashboard Reset: the session is rebuilt under the same demo and session ID."""

    def progress(message: str) -> None:
        _log(job, message)

    token = job.get("token") or _resolve_token(payload, progress)
    job["token"] = token
    wanted = {site.strip().lower() for site in payload.sites if site.strip()}
    wanted_sid = str(payload.session_id or "").strip()
    session_pairs = _session_ref_pairs(payload.sessions)
    use_pairs = session_pairs if session_pairs else None
    reset_count = 0
    attempted = 0
    for dc in job["dcs"]:
        site = dc["site"]
        session_id = dc.get("sessionId") or ""
        if not _dc_matches_action(
            dc,
            session_pairs=use_pairs,
            wanted_sites=wanted,
            wanted_sid=wanted_sid,
        ):
            continue
        if not session_id:
            continue
        if dc.get("phase") in _TERMINAL_DC_PHASES:
            progress(f"{site.upper()}: skip reset ({dc.get('phase')}).")
            continue
        attempted += 1
        result = reset_session(token, site, session_id)
        if result.get("ok"):
            reset_count += 1
            _set_dc(
                job,
                site,
                match_session=session_id,
                phase="resetting",
                status="reset",
                vms=[],
                canReset=False,
                resetPendingUntil=time.time() + RESET_GRACE_SECONDS,
                endedWithoutSave=False,
                # Re-power the same VMs once dCloud brings the session back up.
                powerOnPending=bool(dc.get("powerOnTargets")),
                message="Waiting for dCloud to rebuild this session.",
            )
            progress(f"{site.upper()}: reset session {session_id}; waiting for it to come back.")
        else:
            _set_dc(
                job,
                site,
                match_session=session_id,
                message=result.get("message") or "Reset failed.",
            )
            progress(f"{site.upper()}: reset failed: {result.get('message')}")
    if not attempted:
        job["error"] = "No session to reset."
    elif reset_count:
        job["error"] = ""
    _sync_job_phase(job)
    _persist_job(job)
    _ensure_status_watch(job)


def _extend_job(job: dict[str, Any], payload: ExtendPayload) -> None:
    def progress(message: str) -> None:
        _log(job, message)

    token = job.get("token") or _resolve_token(payload, progress)
    job["token"] = token
    session_pairs = _session_ref_pairs(payload.sessions)
    if not session_pairs:
        job["error"] = "Select at least one session card to extend."
        _persist_job(job)
        return

    stop = parse_schedule_datetime(payload.stop_at)
    if stop is None:
        job["error"] = "Pick a new end date and time for the extension."
        _persist_job(job)
        return
    stop_iso = _dcloud_timestamp(stop)

    targets = [
        dc
        for dc in job.get("dcs") or []
        if _dc_matches_action(
            dc,
            session_pairs=session_pairs,
            wanted_sites=set(),
            wanted_sid="",
        )
        and str(dc.get("sessionId") or "").strip()
        and str(dc.get("phase") or "") == "ready"
    ]
    if not targets:
        job["error"] = "No active session cards to extend — wait until sessions finish starting."
        _persist_job(job)
        return

    progress(f"Extending {len(targets)} session(s) until {stop_iso}…")
    ok_count = 0
    token_box = [token]

    def _one(dc: dict[str, Any]) -> None:
        nonlocal ok_count
        site = str(dc.get("site") or "").lower()
        session_id = str(dc.get("sessionId") or "").strip()
        current_stop = str(dc.get("scheduleStop") or "").strip()
        cur_stop = parse_schedule_datetime(current_stop) if current_stop else None
        if cur_stop is not None and stop <= cur_stop:
            _set_dc(
                job,
                site,
                match_session=session_id,
                message=f"New end must be after current end ({current_stop}).",
            )
            progress(f"{site.upper()}: new end must be after current scheduled end.")
            return
        tok = token_box[0]
        result = extend_session(tok, site, session_id, stop_at=stop_iso)
        if not result.get("ok") and is_auth_error(result.get("message")):
            new_tok, auth_err = _recover_auth(job, progress, tok)
            if auth_err:
                _set_dc(job, site, match_session=session_id, message=auth_err)
                return
            token_box[0] = new_tok
            job["token"] = new_tok
            result = extend_session(new_tok, site, session_id, stop_at=stop_iso)
        if result.get("ok"):
            ok_count += 1
            new_stop = str(result.get("stop") or stop_iso).strip()
            _set_dc(
                job,
                site,
                match_session=session_id,
                scheduleStop=new_stop,
                message=f"Extended until {new_stop}.",
            )
            progress(f"{site.upper()}: session {session_id} extended.")
        else:
            _set_dc(
                job,
                site,
                match_session=session_id,
                message=result.get("message") or "Extend failed.",
            )
            progress(f"{site.upper()}: extend failed: {result.get('message')}")

    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = [pool.submit(_one, dc) for dc in targets]
        for future in as_completed(futures):
            future.result()

    if ok_count:
        progress(f"Extended {ok_count} of {len(targets)} session(s).")
    else:
        job["error"] = "No sessions were extended."
    _persist_job(job)


def _ensure_status_watch(job: dict[str, Any]) -> None:
    if job.get("worker_alive") or job.get("status_watch"):
        return
    pending = [dc for dc in (job.get("dcs") or []) if dc.get("phase") in _WATCHED_DC_PHASES]
    if not pending:
        return
    job["status_watch"] = True

    def _watch() -> None:
        try:
            _watch_job_statuses(job)
        finally:
            job["status_watch"] = False

    threading.Thread(target=_watch, daemon=True).start()
    _log(job, "Watching remaining sessions until they go Active, saved, or ended.")


def _attach_card(
    token: str,
    site: str,
    session_id: str,
    chosen: list[dict[str, Any]],
    *,
    monitor_only: bool = False,
) -> dict[str, Any]:
    vms, details, err = list_session_vms(token, site, session_id)
    if err or not details:
        return {
            "site": site,
            "demoId": "",
            "sessionId": session_id,
            "phase": "error",
            "message": err or "Could not load session.",
            "pool": "",
            "viewUrl": "",
            "status": "",
            "vms": [],
            "savedId": "",
            "contentExport": True,
            "attached": True,
            "monitorOnly": monitor_only,
        }
    # Put attached cards on screen after the initial session call. Live runtime
    # state and WebRDP credentials are filled in by a background worker instead
    # of holding this request for 2+ calls per VM.
    live = tag_selected_vms(list(vms), chosen)
    demo_id = str(details.get("demoId") or details.get("parentId") or "").strip()
    status = details.get("status")
    active = is_active_status(status)
    if monitor_only:
        message = (
            "Monitoring card added — loading VM power and access details in the background."
            if active
            else f"Monitoring ({status}). Move to job workspace when you need bulk save/end."
        )
    else:
        message = (
            "Session card added — loading VM power and access details in the background."
            if active
            else f"Attached existing session ({status}). Wait until Active before save."
        )
    return {
        "site": site,
        "demoId": demo_id,
        "sessionId": session_id,
        "phase": "ready" if active else "waiting",
        "message": message,
        "name": _session_display_name(details),
        "owner": session_owner(details),
        "canReset": bool(details.get("canReset")),
        "pool": str(details.get("poolId") or details.get("pool") or ""),
        "viewUrl": session_view_url(site, session_id, session=details),
        "status": str(status or ""),
        "vms": live,
        "savedId": "",
        "contentExport": True,
        "attached": True,
        "monitorOnly": monitor_only,
    }


def _enrich_attached_card(job: dict[str, Any], site: str, session_id: str) -> None:
    dc = _find_dc(job, site, session_id)
    if not dc:
        return
    error = _load_dc_vms(job, dc, str(job.get("token") or ""))
    if error:
        _log(job, f"{site.upper()}: could not finish attached-card VM details: {error}")
    else:
        if dc.get("phase") == "ready":
            _set_dc(
                job,
                site,
                match_session=session_id,
                message=(
                    "Monitoring — VM power and access details ready. Move to job workspace for bulk save/end controls."
                    if dc.get("monitorOnly")
                    else "Attached existing session — connect, then save or end when finished."
                ),
            )
        _log(job, f"{site.upper()}: attached-card VM power and access details are ready.")
    _persist_job(job)


def _attach_job(payload: AttachPayload) -> dict[str, Any]:
    targets: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in payload.sessions:
        site, session_id = parse_site_and_id(item.session_id, item.site)
        key = (site, session_id)
        if not site or not session_id or key in seen:
            continue
        seen.add(key)
        targets.append(key)
    if not targets:
        raise HTTPException(400, "Select at least one running session to attach.")

    token = _resolve_token(payload)
    chosen = [vm.model_dump() for vm in payload.selected_vms]
    existing: dict[str, Any] | None = None
    job_id = str(payload.job_id or "").strip()
    if job_id:
        with _jobs_lock:
            existing = _jobs.get(job_id)
        if existing is None:
            try:
                existing = _job(job_id)
            except HTTPException:
                existing = None

    if existing is not None:
        job = existing
        job["token"] = token
        job["token_source"] = payload.dcloud_token_source
        job["token_at"] = time.time()
        _log(job, "Attaching additional session(s) to this job.")
    else:
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "phase": "ready_to_patch",
            "createdAt": time.time(),
            "log": [],
            "error": "",
            "stop": threading.Event(),
            "auth_resume": threading.Event(),
            "auth_lock": threading.Lock(),
            "auth_needed": False,
            "auth_message": "",
            "token": token,
            "token_source": payload.dcloud_token_source,
            "token_at": time.time(),
            "selected_vms": chosen,
            "dcs": [],
            "contentExport": True,
        }
        with _jobs_lock:
            _jobs[job_id] = job
        _log(job, f"Job {job_id}: attaching existing sessions (no new schedule).")

    already = {
        (str(dc.get("site") or ""), str(dc.get("sessionId") or ""))
        for dc in (job.get("dcs") or [])
        if dc.get("sessionId")
    }
    added = 0
    skipped = 0
    enrich_cards: list[tuple[str, str]] = []
    for site, session_id in targets:
        if (site, session_id) in already:
            skipped += 1
            _log(job, f"{site.upper()}: session {session_id} is already on the session cards.")
            continue
        card = _attach_card(
            token,
            site,
            session_id,
            chosen,
            monitor_only=bool(payload.monitor_only),
        )
        with _jobs_lock:
            job["dcs"].append(card)
        already.add((site, session_id))
        added += 1
        if card.get("phase") != "error":
            enrich_cards.append((site, session_id))
        live = card.get("vms") or []
        if card.get("phase") == "error":
            _log(job, f"{site.upper()}: attach failed for {session_id}: {card.get('message')}")
            continue
        action = "monitoring" if payload.monitor_only else "attached"
        _log(
            job,
            f"{site.upper()}: {action} session {session_id}"
            + (f" (content {card.get('demoId')})" if card.get("demoId") else "")
            + f" — {sum(1 for vm in live if vm.get('selected'))} selected, {len(live)} total VM(s)"
            + f", {_vm_power_summary(live)}.",
        )

    if not job.get("dcs"):
        raise HTTPException(400, "Could not attach any of the selected sessions.")
    if added == 0:
        if skipped:
            raise HTTPException(
                400,
                f"Selected session(s) are already on this job ({skipped} skipped).",
            )
        raise HTTPException(400, "Could not attach any of the selected sessions.")
    _sync_job_phase(job)
    _ensure_status_watch(job)
    _persist_job(job)
    for site, session_id in enrich_cards:
        threading.Thread(
            target=_enrich_attached_card,
            args=(job, site, session_id),
            daemon=True,
        ).start()
    return job


def _move_card(
    job: dict[str, Any],
    site: str,
    session_id: str,
    *,
    monitor_only: bool,
) -> dict[str, Any]:
    site_code = (site or "").strip().lower()
    sid = str(session_id or "").strip()
    dc = _find_dc(job, site_code, sid)
    if dc is None:
        raise HTTPException(404, "That card is not on this job.")
    dc["monitorOnly"] = bool(monitor_only)
    label = "monitoring" if monitor_only else "job workspace"
    _log(job, f"{site_code.upper()}: moved session {sid} to {label}.")
    _sync_job_phase(job)
    _persist_job(job)
    return job


def _clear_job_workspace(job: dict[str, Any]) -> dict[str, Any]:
    removed = 0
    keep: list[dict[str, Any]] = []
    for dc in job.get("dcs") or []:
        if dc.get("monitorOnly"):
            keep.append(dc)
        else:
            removed += 1
    job["dcs"] = keep
    if removed:
        _log(job, f"Cleared {removed} job workspace card(s) from this tool (monitoring cards kept).")
    _sync_job_phase(job)
    _persist_job(job)
    return job


def _remove_card(
    job: dict[str, Any],
    site: str,
    session_id: str,
    demo_id: str = "",
) -> dict[str, Any]:
    site_code = site.strip().lower()
    sid = str(session_id or "").strip()
    did = str(demo_id or "").strip()
    with _jobs_lock:
        keep = []
        removed = None
        for dc in job.get("dcs") or []:
            if dc.get("site") != site_code:
                keep.append(dc)
                continue
            if sid:
                match = str(dc.get("sessionId") or "") == sid
            elif did:
                match = str(dc.get("demoId") or "") == did
            else:
                match = removed is None
            if match and removed is None:
                removed = dc
                continue
            keep.append(dc)
        job["dcs"] = keep
    if removed is None:
        raise HTTPException(404, "That card is not on this job.")
    _log(
        job,
        f"{site_code.upper()}: removed card from this tool"
        + (f" (session {sid} is still in dCloud)" if sid else "")
        + ".",
    )
    _sync_job_phase(job)
    _persist_job(job)
    return job


def _schedule_saved_job(body: ScheduleSavedPayload) -> dict[str, Any]:
    targets: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in body.items:
        site = (item.site or "").strip().lower()
        content_id = str(item.content_id or "").strip()
        if site not in SITES or not content_id:
            continue
        key = (site, content_id)
        if key in seen:
            continue
        seen.add(key)
        targets.append((site, content_id, str(item.name or "").strip()))
    if not targets:
        raise HTTPException(400, "Select at least one saved content item to schedule.")
    if not body.selected_vms:
        raise HTTPException(
            400,
            "Select VMs in Step 2 (load a session and check VMs), or restore a job that already has them.",
        )

    token = _resolve_token(body)
    kind = "exported" if body.content_export else "regular"
    job_id = str(body.job_id or "").strip()
    existing: dict[str, Any] | None = None
    if job_id:
        with _jobs_lock:
            existing = _jobs.get(job_id)
        if existing is None:
            try:
                existing = _job(job_id)
            except HTTPException:
                existing = None

    if existing is not None:
        job = existing
        job["token"] = token
        job["token_source"] = body.dcloud_token_source
        job["token_at"] = time.time()
        job["selected_vms"] = [vm.model_dump() for vm in body.selected_vms]
        job["contentExport"] = body.content_export
        _log(job, f"Scheduling {len(targets)} saved content item(s) on this job.")
    else:
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "phase": "scheduling",
            "createdAt": time.time(),
            "log": [],
            "error": "",
            "stop": threading.Event(),
            "auth_resume": threading.Event(),
            "auth_lock": threading.Lock(),
            "auth_needed": False,
            "auth_message": "",
            "token": token,
            "token_source": body.dcloud_token_source,
            "token_at": time.time(),
            "selected_vms": [vm.model_dump() for vm in body.selected_vms],
            "contentExport": body.content_export,
            "dcs": [],
        }
        with _jobs_lock:
            _jobs[job_id] = job
        _log(
            job,
            f"Job {job_id}: scheduling {kind} sessions from {len(targets)} saved content ID(s).",
        )

    for site, content_id, name in targets:
        job["dcs"].append(
            {
                "site": site,
                "demoId": content_id,
                "sessionId": "",
                "phase": "scheduling",
                "message": f"From saved content {content_id}…",
                "name": name,
                "pool": "",
                "viewUrl": "",
                "status": "",
                "vms": [],
                "savedId": "",
                "contentExport": body.content_export,
            }
        )

    run_payload = RunPayload(
        dcloud_token=body.dcloud_token,
        dcloud_token_source=body.dcloud_token_source,
        demo_ids=DemoIds(),
        selected_vms=body.selected_vms,
        days=body.days,
        start_at=body.start_at,
        stop_at=body.stop_at,
        active_timeout_minutes=body.active_timeout_minutes,
        content_export=body.content_export,
        auto_next_available=body.auto_next_available,
        schedule_decisions=body.schedule_decisions,
    )
    if job.get("worker_alive"):
        threading.Thread(
            target=_schedule_and_watch_new_dcs,
            args=(job, run_payload),
            daemon=True,
        ).start()
    else:
        threading.Thread(target=_run_job, args=(job, run_payload), daemon=True).start()
    _persist_job(job)
    return _public_job(job)


@app.post("/api/schedule/conflicts")
def api_schedule_conflicts(body: ScheduleConflictCheckPayload) -> dict[str, Any]:
    targets = _schedule_targets_from_conflict_body(body)
    if not targets:
        raise HTTPException(400, "Enter at least one demo / content ID to check.")
    token = _resolve_token(body, lambda _msg: None)
    conflicts: list[dict[str, Any]] = []
    for site, demo_id in targets:
        hit = find_schedule_conflict(
            token,
            site,
            demo_id,
            days=body.days,
            start_at=body.start_at,
            stop_at=body.stop_at,
        )
        if hit:
            conflicts.append(hit)
    return {"conflicts": conflicts}


@app.post("/api/jobs")
def api_start_job(body: RunPayload) -> dict[str, Any]:
    targets = _demo_targets(body.demo_ids)
    if not targets:
        raise HTTPException(
            400,
            "Enter at least one demo / content ID (SJC, RTP, LON, SNG, and/or SYD).",
        )
    if not body.selected_vms and not body.skip_power_on:
        raise HTTPException(400, "Select at least one VM to power on.")

    kind = "exported" if body.content_export else "regular"
    append_job_id = str(body.job_id or "").strip()
    existing: dict[str, Any] | None = None
    if append_job_id:
        with _jobs_lock:
            existing = _jobs.get(append_job_id)
        if existing is None:
            try:
                existing = _job(append_job_id)
            except HTTPException:
                existing = None
        if existing is None:
            raise HTTPException(404, "Job not found — restore the last job or start a new one.")

    if existing is not None:
        job = existing
        job["selected_vms"] = [vm.model_dump() for vm in body.selected_vms]
        job["contentExport"] = body.content_export
        added = _append_demo_schedule_to_job(job, targets, content_export=body.content_export)
        if not added:
            raise HTTPException(400, "No demo IDs to schedule.")
        _persist_job(job)
        threading.Thread(target=_schedule_and_watch_new_dcs, args=(job, body), daemon=True).start()
        return _public_job(job)

    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "phase": "scheduling",
        "createdAt": time.time(),
        "log": [],
        "error": "",
        "stop": threading.Event(),
        "auth_resume": threading.Event(),
        "auth_lock": threading.Lock(),
        "auth_needed": False,
        "auth_message": "",
        "token_source": body.dcloud_token_source,
        "selected_vms": [vm.model_dump() for vm in body.selected_vms],
        "contentExport": body.content_export,
        "dcs": [
            _new_demo_dc_card(site, demo_id, content_export=body.content_export)
            for site, demo_id in targets
        ],
    }
    with _jobs_lock:
        _jobs[job_id] = job
    _log(job, f"Job {job_id}: {kind} sessions for {', '.join(site.upper() for site, _ in targets)}.")
    threading.Thread(target=_run_job, args=(job, body), daemon=True).start()
    return _public_job(job)


@app.get("/api/jobs/latest/preview")
def api_latest_job_preview() -> dict[str, Any]:
    return _last_job_preview()


@app.get("/api/jobs/latest")
def api_latest_job() -> dict[str, Any]:
    with _jobs_lock:
        jobs = list(_jobs.values())
    if not jobs:
        _load_last_job()
        with _jobs_lock:
            jobs = list(_jobs.values())
    if not jobs:
        raise HTTPException(404, "No saved job to restore.")
    jobs.sort(key=lambda item: _job_activity_ts(item), reverse=True)
    return _public_job(jobs[0])


@app.post("/api/jobs/attach")
def api_attach_job(body: AttachPayload) -> dict[str, Any]:
    return _public_job(_attach_job(body))


@app.post("/api/sessions/resolve-monitor")
def api_resolve_monitor(body: ResolveMonitorPayload) -> dict[str, Any]:
    token = _resolve_token(body)
    sessions, err = resolve_monitor_sessions(token, body.site, body.identifier)
    if err:
        raise HTTPException(400, err)
    if not sessions:
        raise HTTPException(
            404,
            "No running session matched that demo ID or session ID in the selected DC.",
        )
    return {"ok": True, "sessions": sessions}


def _discard_job(job: dict[str, Any]) -> None:
    """Stop this job's threads and stop it writing last-job.json."""
    job["discarded"] = True
    for key in ("stop", "auth_resume"):
        event = job.get(key)
        if isinstance(event, threading.Event):
            event.set()


def _drop_local_cards(*, workspace: bool, monitoring: bool) -> None:
    """Forget job workspace cards, monitoring cards, or both. dCloud is untouched.

    Watchers hold a reference to the job and keep writing last-job.json, so a full
    clear signals them to stop before the file goes away.
    """
    with _jobs_lock:
        jobs = list(_jobs.values())
    for job in jobs:
        if workspace and monitoring:
            _discard_job(job)
            continue
        # Partial clear: keep the kind that was not checked.
        keep_monitor_cards = not monitoring
        job["dcs"] = [
            dc
            for dc in (job.get("dcs") or [])
            if bool(dc.get("monitorOnly")) == keep_monitor_cards
        ]
        if job["dcs"]:
            _sync_job_phase(job)
            _persist_job(job)
        else:
            job_id = str(job.get("id") or "")
            _discard_job(job)
            with _jobs_lock:
                _jobs.pop(job_id, None)
            _unlink_last_job(job_id)
    if workspace and monitoring:
        with _jobs_lock:
            _jobs.clear()
        _unlink_last_job()


@app.post("/api/jobs/reset")
def api_reset_local(body: LocalResetPayload | None = None) -> dict[str, Any]:
    """Forget local records. Does not end or save dCloud sessions."""
    scopes = body or LocalResetPayload()
    if scopes.job or scopes.monitoring:
        _drop_local_cards(workspace=scopes.job, monitoring=scopes.monitoring)
    if scopes.log:
        # Otherwise the next card render replays the job's stored log lines.
        with _jobs_lock:
            jobs = list(_jobs.values())
        for job in jobs:
            job["log"] = []
            _persist_job(job)
    if scopes.hub:
        _persist_managed_saved_state(_empty_managed_state())
        try:
            (APP_DIR / "last-saved-ids.json").unlink()
        except OSError:
            pass
    return {
        "ok": True,
        "cleared": True,
        "job": scopes.job,
        "monitoring": scopes.monitoring,
        "hub": scopes.hub,
        "log": scopes.log,
    }


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: str) -> dict[str, Any]:
    return _public_job(_job(job_id))


@app.post("/api/jobs/{job_id}/refresh-status")
def api_refresh_job_status(job_id: str, body: RefreshStatusPayload) -> dict[str, Any]:
    job = _job(job_id)
    token = _resolve_token(body)
    _copy_session_to_job(job)
    job["token"] = token
    job["token_source"] = body.dcloud_token_source
    job["token_at"] = time.time()
    site = body.site.strip().lower()
    session_id = str(body.session_id or "").strip()
    if site:
        dc = _find_dc(job, site, session_id or None)
        if not dc:
            raise HTTPException(404, f"No card for {site.upper()}.")
        _log(job, f"Refreshing {site.upper()} session status from dCloud…")
        _refresh_dc_from_dcloud(job, dc, token)
        _sync_job_phase(job)
    else:
        _log(job, "Refreshing live session status from dCloud…")
        _refresh_job_from_dcloud(job, token)
    _ensure_status_watch(job)
    return _public_job(job)


@app.post("/api/jobs/{job_id}/stop")
def api_stop_job(job_id: str) -> dict[str, Any]:
    job = _job(job_id)
    job["stop"].set()
    resume = job.get("auth_resume")
    if resume is not None:
        resume.set()
    _log(job, "Stop requested.")
    return _public_job(job)


@app.post("/api/jobs/{job_id}/dismiss")
def api_dismiss_job(job_id: str) -> dict[str, Any]:
    return _dismiss_job(job_id)


@app.post("/api/jobs/{job_id}/remove-card")
def api_remove_card(job_id: str, body: RemoveCardPayload) -> dict[str, Any]:
    job = _job(job_id)
    return _public_job(_remove_card(job, body.site, body.session_id, body.demo_id))


@app.post("/api/jobs/{job_id}/move-card")
def api_move_card(job_id: str, body: MoveCardPayload) -> dict[str, Any]:
    job = _job(job_id)
    return _public_job(
        _move_card(job, body.site, body.session_id, monitor_only=body.monitor_only)
    )


@app.post("/api/jobs/{job_id}/clear-workspace")
def api_clear_workspace(job_id: str) -> dict[str, Any]:
    job = _job(job_id)
    return _public_job(_clear_job_workspace(job))


@app.post("/api/jobs/{job_id}/resume-auth")
def api_resume_auth(job_id: str, body: TokenPayload) -> dict[str, Any]:
    job = _job(job_id)
    token = _resolve_token(body)
    job["token"] = token
    job["token_source"] = body.dcloud_token_source
    job["token_at"] = time.time()
    _copy_session_to_job(job)
    job["auth_needed"] = False
    job["auth_message"] = ""
    if job.get("phase") == "paused_auth":
        job["phase"] = job.get("phase_before_pause") or "waiting_active"
    resume = job.setdefault("auth_resume", threading.Event())
    resume.set()
    _log(job, "Token updated — continuing.")
    return _public_job(job)


def _verify_guest_shutdown(
    job: dict[str, Any],
    site: str,
    session_id: str,
    target: dict[str, Any],
) -> None:
    """Keep a VM powered on in the UI until dCloud confirms it is off."""
    result = wait_for_power_state(
        str(job.get("token") or ""),
        site,
        session_id,
        [target],
        want_on=False,
        timeout_seconds=5 * 60,
        poll_seconds=10,
        should_stop=job["stop"].is_set,
    )
    verified = bool(result.get("ok"))
    observed = (result.get("vms") or [{}])[0]
    with _jobs_lock:
        dc = _find_dc(job, site, session_id)
        if not dc:
            return
        for vm in dc.get("vms") or []:
            same_mor = target.get("mor") and vm.get("mor") == target.get("mor")
            same_uid = target.get("uid") and vm.get("uid") == target.get("uid")
            same_name = target.get("name") and vm.get("name") == target.get("name")
            if not (same_mor or same_uid or same_name):
                continue
            vm["shutdownPending"] = False
            if verified:
                vm["powerState"] = observed.get("powerState") or "Powered Off"
                vm["guestState"] = observed.get("guestState") or vm.get("guestState") or ""
                vm["lastAction"] = "Guest shutdown verified — VM is powered off."
            else:
                vm["lastAction"] = result.get("message") or (
                    "Guest shutdown was accepted, but powered-off state was not verified."
                )
            break
    _log(
        job,
        f"{site.upper()}: guest shutdown {target.get('name') or target.get('mor')} "
        + ("verified powered off." if verified else f"not verified: {result.get('message') or 'timed out'}."),
    )
    _persist_job(job)


@app.post("/api/jobs/{job_id}/vm-action")
def api_vm_action(job_id: str, body: VmActionPayload) -> dict[str, Any]:
    job = _job(job_id)
    site = (body.site or "").strip().lower()
    action = (body.action or "guestShutdown").strip()
    allowed = {
        "guestShutdown": ("Guest shutdown", "guest shutdown requested"),
        "vmPowerOn": ("Power on", "Powered On"),
        "vmPowerOff": ("Power off", "Powered Off"),
    }
    if action not in allowed:
        raise HTTPException(400, "Supported VM actions: power on, power off, guest shutdown.")
    label, power_state = allowed[action]
    dc = _find_dc(job, site, body.session_id)
    if not dc:
        raise HTTPException(400, f"No job data for {site.upper()}.")
    session_id = dc.get("sessionId") or ""
    if not session_id:
        raise HTTPException(400, f"{site.upper()} does not have a session yet.")
    token = job.get("token") or _resolve_token(body)
    target = {"name": body.name, "mor": body.mor, "uid": body.uid}
    result = vm_action(token, site, session_id, target, action)
    _log(job, f"{site.upper()}: {label.lower()} {body.name or body.mor}: {result.get('message')}")
    with _jobs_lock:
        for vm in dc.get("vms") or []:
            same_mor = body.mor and vm.get("mor") == body.mor
            same_uid = body.uid and vm.get("uid") == body.uid
            same_name = body.name and vm.get("name") == body.name
            if same_mor or same_uid or same_name:
                vm["lastAction"] = result.get("message") or ""
                if result.get("ok"):
                    if action == "guestShutdown":
                        vm["shutdownPending"] = True
                        vm["lastAction"] = "Guest shutdown requested — waiting for powered-off confirmation."
                    else:
                        vm["powerState"] = power_state
                break
    if result.get("ok") and action == "guestShutdown":
        threading.Thread(
            target=_verify_guest_shutdown,
            args=(job, site, session_id, target),
            daemon=True,
        ).start()
    return {"ok": bool(result.get("ok")), "result": result, "job": _public_job(job)}


@app.post("/api/jobs/{job_id}/rename-session")
def api_rename_session(job_id: str, body: RenameSessionPayload) -> dict[str, Any]:
    job = _job(job_id)
    site = (body.site or "").strip().lower()
    if site not in SITES:
        raise HTTPException(400, "Datacenter must be SJC, RTP, LON, SNG, or SYD.")
    session_id = str(body.session_id or "").strip()
    if not session_id:
        raise HTTPException(400, "Session ID is required.")
    dc = _find_dc(job, site, session_id)
    if not dc:
        raise HTTPException(400, f"No card found for {site.upper()} session {session_id}.")
    if str(dc.get("phase") or "") in {"saved", "ended"}:
        raise HTTPException(400, "Only live sessions can be renamed.")
    new_name = str(body.name or "").strip()
    if not new_name:
        raise HTTPException(400, "Session name is required.")
    token = job.get("token") or _resolve_token(body)
    result = update_session_name(token, site, session_id, new_name)
    if not result.get("ok"):
        raise HTTPException(400, result.get("message") or "Rename failed.")
    resolved = str(result.get("name") or new_name).strip() or new_name
    _set_dc(job, site, match_session=session_id, name=resolved, savedName=resolved)
    _persist_job(job)
    _log(job, f"{site.upper()}: renamed session {session_id} to {resolved!r}.")
    return {"ok": True, "result": result, "job": _public_job(job)}


@app.post("/api/jobs/{job_id}/shutdown-save")
def api_shutdown_save(job_id: str, body: ShutdownPayload) -> dict[str, Any]:
    job = _job(job_id)
    body.job_id = job_id
    candidates = list(_iter_save_candidates(job, body))
    # Saving rewrites the owner's content, so only the signed-in owner may do it.
    not_mine = [dc for dc in candidates if _dc_owned_by_me(job, dc) is False]
    for dc in not_mine:
        _log(job, f"{dc['site'].upper()}: skip save — session belongs to {dc.get('owner') or 'someone else'}.")
    mine = [dc for dc in candidates if dc not in not_mine]
    targets = [dc for dc in mine if _claim_dc_for_save(job, dc)]
    if not targets:
        if not_mine:
            raise HTTPException(400, _not_my_sessions_message(not_mine, "save"))
        stale = [
            dc
            for dc in (job.get("dcs") or [])
            if _dc_can_save(dc) and _dc_too_old_for_bulk(job, dc)
        ]
        if stale and not body.single_card:
            labels = ", ".join(
                f"{str(dc.get('site') or '').upper()} {dc.get('sessionId') or ''}" for dc in stale
            )
            raise HTTPException(
                400,
                f"Skipped {labels} — left over from an earlier run, so a bulk save leaves it "
                "alone. Save it from its own card if you really want it.",
            )
        raise HTTPException(
            400,
            "No ready sessions to save. Already-saved and in-progress sessions are skipped.",
        )
    _persist_job(job)
    threading.Thread(target=_shutdown_job, args=(job, body, targets), daemon=True).start()
    return _public_job(job)


@app.post("/api/jobs/{job_id}/end-sessions")
def api_end_sessions(job_id: str, body: EndPayload) -> dict[str, Any]:
    job = _job(job_id)
    body.job_id = job_id
    threading.Thread(target=_end_job, args=(job, body), daemon=True).start()
    return _public_job(job)


@app.post("/api/jobs/{job_id}/reset-sessions")
def api_reset_sessions(job_id: str, body: ResetPayload) -> dict[str, Any]:
    job = _job(job_id)
    body.job_id = job_id
    _reset_job(job, body)
    return _public_job(job)


@app.post("/api/jobs/{job_id}/extend-sessions")
def api_extend_sessions(job_id: str, body: ExtendPayload) -> dict[str, Any]:
    job = _job(job_id)
    threading.Thread(target=_extend_job, args=(job, body), daemon=True).start()
    return _public_job(job)


def _unified_content_result(site: str, item: dict[str, Any]) -> dict[str, Any]:
    content_id = str(item.get("demoId") or item.get("uid") or "").strip()
    state = item.get("state") or []
    state_text = " / ".join(str(part) for part in state) if isinstance(state, list) else str(state)
    return {
        "source": "content",
        "site": site.upper(),
        "id": content_id,
        "name": str(item.get("name") or "").strip(),
        "owner": str(item.get("owner") or "").strip(),
        "status": state_text or str(item.get("type") or ""),
        "type": str(item.get("type") or ""),
        "filterIds": item.get("filterIds") if isinstance(item.get("filterIds"), list) else [],
        "updated": str(item.get("updated") or ""),
        "topologyUrl": edit_topology_url(site, content_id, item),
        "scheduleId": content_id,
    }


def _unified_session_result(site: str, item: dict[str, Any]) -> dict[str, Any]:
    session_id = str(item.get("uid") or "").strip()
    event = item.get("event") if isinstance(item.get("event"), dict) else {}
    return {
        "source": "session",
        "site": site.upper(),
        "id": session_id,
        "name": str(item.get("name") or item.get("parentDemoName") or "").strip(),
        "owner": str(item.get("owner") or "").strip(),
        "status": format_status(item.get("status"), ""),
        "start": str(item.get("start") or ""),
        "stop": str(item.get("stop") or ""),
        # Demo ID is the parent content this session was scheduled from.
        "demoId": str(item.get("parentId") or "").strip(),
        "eventId": str((event or {}).get("uid") or "").strip(),
        "eventName": str((event or {}).get("name") or "").strip(),
        "contentPool": str(item.get("contentPoolName") or "").strip(),
        "vc": str(item.get("virtualCenter") or "").strip(),
        "savedId": str(item.get("activeId") or "").strip(),
        "type": str(item.get("type") or "").strip(),
        "viewUrl": session_view_url(site, session_id, session=item),
    }


@app.post("/api/search/content-details")
def api_unified_content_details(body: SearchItemPayload) -> dict[str, Any]:
    site = str(body.site or "").strip().lower()
    content_id = str(body.content_id or "").strip()
    if site not in SITES or not content_id:
        raise HTTPException(400, "Datacenter and content ID are required.")
    token = _resolve_token(body)
    records, list_error = fetch_admin_records(token, site, resource="demos")
    selected_filter_ids: list[Any] = []
    for item in records:
        item_id = str(item.get("demoId") or item.get("uid") or "").strip()
        if item_id == content_id:
            raw_ids = item.get("filterIds")
            selected_filter_ids = raw_ids if isinstance(raw_ids, list) else []
            break
    panels, errors = fetch_admin_content_panels(
        token,
        site,
        content_id,
        selected_filter_ids=selected_filter_ids,
    )
    if list_error:
        errors["content"] = list_error
    return {
        "ok": True,
        "site": site.upper(),
        "contentId": content_id,
        **panels,
        "errors": errors,
    }


@app.post("/api/search/session-action")
def api_unified_session_action(body: SearchItemPayload) -> dict[str, Any]:
    site = str(body.site or "").strip().lower()
    session_id = str(body.session_id or "").strip()
    action = str(body.action or "").strip().lower()
    if site not in SITES or not session_id:
        raise HTTPException(400, "Datacenter and session ID are required.")
    token = _resolve_token(body)
    if action == "rename":
        result = update_session_name(token, site, session_id, body.name)
    elif action == "edit":
        result = update_session_schedule(
            token,
            site,
            session_id,
            name=body.name,
            start_at=body.start_at,
            stop_at=body.stop_at,
        )
    elif action in {"end", "cancel"}:
        # dCloud's End API is also the backend operation behind Cancel for a
        # session which has not started yet; the UI changes the label by status.
        result = end_session(token, site, session_id)
    elif action == "reset":
        result = reset_session(token, site, session_id)
    elif action == "save":
        result = save_session(
            token,
            site,
            session_id,
            name=body.name,
            description=body.description,
        )
    else:
        raise HTTPException(400, "Unsupported session action.")
    if not result.get("ok"):
        raise HTTPException(400, result.get("message") or f"Could not {action} session.")
    return {"ok": True, "action": action, **result}


@app.post("/api/search/session-log")
def api_unified_session_log(body: SearchItemPayload) -> dict[str, Any]:
    site = str(body.site or "").strip().lower()
    session_id = str(body.session_id or "").strip()
    if site not in SITES or not session_id:
        raise HTTPException(400, "Datacenter and session ID are required.")
    token = _resolve_token(body)
    text, error = fetch_session_log(token, site, session_id)
    if error:
        raise HTTPException(400, error)
    return {
        "ok": True,
        "site": site.upper(),
        "sessionId": session_id,
        "log": text or "dCloud returned an empty log for this session.",
    }


@app.post("/api/session/info")
def api_session_info(body: SearchItemPayload) -> dict[str, Any]:
    """Session Details panels, the same ones dCloud shows on its session page."""
    site = str(body.site or "").strip().lower()
    session_id = str(body.session_id or "").strip()
    if site not in SITES or not session_id:
        raise HTTPException(400, "Datacenter and session ID are required.")
    token = _resolve_token(body)
    info, error = session_info_panels(token, site, session_id)
    if error:
        raise HTTPException(400, error)
    return {"ok": True, **info}


@app.post("/api/search/dc-data")
def api_unified_dc_data(body: UnifiedSearchPayload) -> dict[str, Any]:
    requested_sites = body.sites or [body.site]
    sites = [
        site
        for site in SITES
        if site in {str(value or "").strip().lower() for value in requested_sites}
    ]
    if not sites:
        raise HTTPException(400, "Select at least one datacenter.")
    wanted = {str(value or "").strip().lower() for value in body.sources}
    sources = [source for source in ("content", "sessions") if source in wanted]
    if not sources:
        raise HTTPException(400, "Select Content or Sessions to load.")
    token = _resolve_token(body)
    records: dict[tuple[str, str], list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(sites) * len(sources)) as pool:
        futures = {
            pool.submit(
                fetch_admin_records,
                token,
                site,
                resource="demos" if source == "content" else "sessions",
                refresh=body.refresh_data,
            ): (source, site)
            for site in sites
            for source in sources
        }
        for future, (source, site) in futures.items():
            items, error = future.result()
            records[(source, site)] = items
            if error:
                errors[f"{source}:{site}"] = error
    return {
        "ok": True,
        "sites": [site.upper() for site in sites],
        "sources": sources,
        "content": [
            _unified_content_result(site, item)
            for site in sites
            for item in records.get(("content", site), [])
        ],
        "sessions": [
            _unified_session_result(site, item)
            for site in sites
            for item in records.get(("sessions", site), [])
        ],
        "errors": errors,
        "fetchedAt": {
            f"{source}:{site}": admin_records_cached_at(
                site,
                resource="demos" if source == "content" else "sessions",
            )
            for source in sources
            for site in sites
            if not errors.get(f"{source}:{site}")
        },
        "cacheSeconds": 15 * 60,
    }


@app.post("/api/search/all")
def api_unified_search(body: UnifiedSearchPayload) -> dict[str, Any]:
    query = str(body.query or "").strip()
    if not query:
        raise HTTPException(400, "Enter a name or ID to search.")
    requested_sites = body.sites or [body.site]
    sites = [
        site
        for site in SITES
        if site in {str(value or "").strip().lower() for value in requested_sites}
    ]
    if not sites:
        raise HTTPException(400, "Select at least one datacenter.")
    requested_sources = {str(value or "").strip().lower() for value in body.sources}
    sources = [
        source
        for source in ("catalog", "content", "sessions")
        if not requested_sources or source in requested_sources
    ]
    if not sources:
        raise HTTPException(400, "Select Catalog, Content, or Sessions to search.")
    catalog_site = sites[0]
    token = _resolve_token(body)
    strategy = "SUBSTRING" if body.exact_catalog else "RELEVANCY"
    catalog_items: list[dict[str, Any]] = []
    content_by_site: dict[str, list[dict[str, Any]]] = {}
    sessions_by_site: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    dc_sources = [source for source in sources if source != "catalog"]
    with ThreadPoolExecutor(max_workers=1 + len(sites) * max(1, len(dc_sources))) as pool:
        catalog_future = (
            pool.submit(catalog_search, token, catalog_site, query, strategy)
            if "catalog" in sources
            else None
        )
        futures: dict[Any, tuple[str, str]] = {}
        for site in sites:
            for source in dc_sources:
                futures[
                    pool.submit(
                        search_admin_records,
                        token,
                        site,
                        query,
                        resource="demos" if source == "content" else "sessions",
                        limit=50,
                        refresh=body.refresh_data,
                    )
                ] = (source, site)
        if catalog_future is not None:
            catalog_items, catalog_error = catalog_future.result()
            if catalog_error:
                errors["catalog"] = catalog_error
        for future, (source, site) in futures.items():
            items, error = future.result()
            if source == "content":
                content_by_site[site] = items
            else:
                sessions_by_site[site] = items
            if error:
                errors[f"{source}:{site}"] = error

    catalog = [
        {
            "source": "catalog",
            "id": str(item.get("id") or "").strip(),
            "name": str(item.get("name") or "").strip(),
            "contentType": str(item.get("contentType") or "").strip(),
            "updated": str(
                item.get("modifiedOn") or item.get("updatedOn") or item.get("publishedOn") or ""
            ),
            "profileUrl": (
                f"{site_base(catalog_site)}/demo/{quote(str(item.get('id') or '').strip())}"
                if item.get("id")
                else ""
            ),
        }
        for item in catalog_items[:30]
        if isinstance(item, dict)
    ]
    return {
        "ok": True,
        "query": query,
        "site": sites[0].upper() if len(sites) == 1 else "MULTI",
        "sites": [site.upper() for site in sites],
        "sources": sources,
        "catalog": catalog,
        "content": [
            _unified_content_result(site, item)
            for site in sites
            for item in content_by_site.get(site, [])
        ],
        "sessions": [
            _unified_session_result(site, item)
            for site in sites
            for item in sessions_by_site.get(site, [])
        ],
        "errors": errors,
        "cacheSeconds": 15 * 60,
    }


@app.post("/api/search/catalog-ids")
def api_unified_catalog_ids(body: CatalogIdsPayload) -> dict[str, Any]:
    name = str(body.name or "").strip()
    if not name:
        raise HTTPException(400, "Catalog name is required.")
    token = _resolve_token(body)
    lookups = lookup_demo_ids_across_sites(token, name)
    ids = {
        site: str((lookups.get(site) or {}).get("id") or "").strip()
        for site in SITES
        if str((lookups.get(site) or {}).get("id") or "").strip()
    }
    if not ids:
        raise HTTPException(404, "That catalog result has no schedulable demo IDs.")
    return {"ok": True, "name": name, "ids": ids, "lookups": lookups}


@app.post("/api/sessions/mine")
def api_my_sessions(body: TokenPayload) -> dict[str, Any]:
    token = _resolve_token(body)
    result = list_dashboard_sessions_all_sites(token)
    return {"ok": True, **result}


def _share_kind(body: ShareStatePayload | ShareUpdatePayload | ShareSearchPayload) -> str:
    kind = str(body.kind or "session").strip().lower()
    return "content" if kind == "content" else "session"


def _apply_share_to_job_dc(
    job: dict[str, Any],
    *,
    site: str,
    kind: str,
    session_id: str,
    content_id: str,
    shared_with: list[dict[str, str]],
) -> None:
    site_code = site.strip().lower()
    sid = str(session_id or "").strip()
    cid = str(content_id or "").strip()
    for dc in job.get("dcs") or []:
        if str(dc.get("site") or "").lower() != site_code:
            continue
        if kind == "content":
            if cid and str(dc.get("savedId") or "").strip() == cid:
                dc["sharedWith"] = shared_with
        elif sid and str(dc.get("sessionId") or "").strip() == sid:
            dc["sharedWith"] = shared_with


def _connect_cai(cookie: str = "") -> dict[str, Any]:
    if not host_resolves(CAI_HOST):
        message = off_network_message("CAI")
        _set_cai_cookie("", message)
        raise HTTPException(401, message)
    header = (cookie or "").strip()
    probed = probe_cai_login(header)
    if not probed.get("loggedIn") and header:
        probed = probe_cai_login("")
        header = ""
    if not probed.get("loggedIn"):
        raise HTTPException(
            401,
            probed.get("message")
            or "CAI is not reachable from this machine. Join the Cisco network and click Connect to CAI.",
        )
    message = probed.get("message") or "CAI session is active."
    if not header:
        message = "CAI session is active — reachable on this Cisco network."
    _mark_cai_reachable(message, header)
    return _cai_public_status({"loggedIn": True, "ok": True, "message": message})


@app.get("/api/cai/status")
def api_cai_status() -> dict[str, Any]:
    # No cookie is needed on the Cisco network, so probe instead of waiting for a click.
    return _cai_auto_connect()


@app.post("/api/cai/connect")
def api_cai_connect() -> dict[str, Any]:
    return _connect_cai(_cai_cookie())


@app.post("/api/cai/import-chrome")
def api_cai_import_chrome() -> dict[str, Any]:
    cookie, message = import_cai_cookies_from_chrome()
    if not cookie:
        return _connect_cai("")
    return _connect_cai(cookie)


@app.post("/api/cai/import-cookie")
def api_cai_import_cookie(body: CaiCookiePayload) -> dict[str, Any]:
    cookie = normalize_cookie_header(body.cookie)
    if cookie_header_is_tracking_only(cookie):
        return _connect_cai("")
    if not cookie or "=" not in cookie:
        return _connect_cai("")
    return _connect_cai(cookie)


@app.post("/api/cai/vms")
def api_cai_vms(body: CaiVmsPayload) -> dict[str, Any]:
    cookie = _require_cai_cookie()
    refs: list[CaiDemoRef] = list(body.items or [])
    if body.site and body.saved_id:
        first = CaiDemoRef(site=body.site, saved_id=body.saved_id)
        if not any(
            str(item.site).lower() == first.site.lower() and str(item.saved_id) == first.saved_id
            for item in refs
        ):
            refs.insert(0, first)
    if not refs:
        raise HTTPException(400, "Select at least one saved content row.")
    names: list[str] = []
    seen: set[str] = set()
    integrate_dcs: list[dict[str, str]] = []
    seen_dcs: set[str] = set()
    integrated_dcs: list[dict[str, str]] = []
    seen_integrated: set[str] = set()
    last_ok: dict[str, Any] | None = None
    last_err = "Could not load VMs from CAI."
    listed = list_cai_tasks(cookie)
    home_tasks = listed.get("tasks") or [] if listed.get("ok") else []
    for ref in refs:
        result = fetch_demo_page(cookie, ref.site, ref.saved_id)
        if result.get("loggedIn") is False:
            _set_cai_cookie("", result.get("message") or "")
            raise HTTPException(401, result.get("message") or "CAI session expired.")
        if not result.get("ok"):
            last_err = str(result.get("message") or last_err)
            continue
        last_ok = result
        for name in result.get("vms") or []:
            label = str(name).strip()
            if label and label not in seen:
                seen.add(label)
                names.append(label)
        for dc in result.get("integrateDcs") or []:
            code = normalize_cai_dc(str(dc or ""))
            if code and code not in seen_dcs:
                seen_dcs.add(code)
                integrate_dcs.append({"id": code, "label": cai_dc_label(code)})
        page_tasks = list(result.get("tasks") or []) + list(home_tasks)
        for task in match_integrate_task(page_tasks, saved_id=str(ref.saved_id or "")):
            status = normalize_task_status(str(task.get("status") or ""))
            _source, dest = parse_cai_task_dc(str(task.get("dc") or ""))
            if not dest or dest in seen_integrated:
                continue
            if status != "completed":
                continue
            seen_integrated.add(dest)
            integrated_dcs.append({"id": dest, "label": cai_dc_label(dest)})
    if last_ok is None:
        raise HTTPException(400, last_err)
    last_ok = dict(last_ok)
    last_ok["vms"] = names
    last_ok["integrateDcs"] = integrate_dcs
    last_ok["integratedDcs"] = integrated_dcs
    _mark_cai_reachable("CAI session is active — reachable on this Cisco network.", cookie)
    return last_ok


@app.post("/api/cai/replace")
def api_cai_replace(body: CaiReplacePayload) -> dict[str, Any]:
    cookie = _require_cai_cookie()
    shared_vms = [str(name).strip() for name in body.vms if str(name).strip()]
    if not body.items:
        raise HTTPException(400, "Select at least one saved content row.")
    job: dict[str, Any] | None = None
    job_id = str(body.job_id or "").strip()
    if job_id:
        try:
            job = _job(job_id)
        except HTTPException:
            job = None
    submitted: list[dict[str, Any]] = []
    errors: list[str] = []
    for item in body.items:
        site = (item.site or "").strip().lower()
        saved_id = str(item.saved_id or "").strip()
        target_id = str(item.target_id or "").strip()
        vms = [str(name).strip() for name in (item.vms or shared_vms) if str(name).strip()]
        if not site or not saved_id:
            continue
        if not vms:
            errors.append(f"{site.upper()} {saved_id}: no VMs selected.")
            continue
        if not target_id:
            target_id = _lookup_published_id(site, saved_id)
            if target_id:
                _upsert_managed_saved_rows(
                    [{"site": site, "savedId": saved_id, "publishedId": target_id, "parentId": target_id}]
                )
        if not target_id:
            errors.append(
                f"{site.upper()} {saved_id}: no parent ID. VM replacement is only for content saved from a parent demo."
            )
            continue
        page = fetch_demo_page(cookie, site, saved_id)
        cai_dc = str(page.get("caiDc") or "")
        result = submit_vm_replace(
            cookie,
            site,
            saved_id,
            vm_names=vms,
            target_demoid=target_id,
            cai_dc=cai_dc,
        )
        if result.get("loggedIn") is False:
            _set_cai_cookie("", result.get("message") or "")
            raise HTTPException(401, result.get("message") or "CAI session expired.")
        submit_status = "submitted" if result.get("ok") else "error"
        row = {
            "site": site,
            "savedId": saved_id,
            "targetId": target_id,
            "vms": vms,
            "status": submit_status,
            "message": result.get("message") or "",
            "caiUrl": result.get("caiUrl") or cai_demo_url(site, saved_id),
            "caiDc": result.get("caiDc") or cai_dc,
            "newId": "",
            # One CAI task per VM, so seed a pill per VM right away; the refresh
            # poll fills in each VM's real status.
            "vmTasks": cai_replace_vm_chips(
                vms,
                [],
                saved_id=saved_id,
                target_id=target_id,
                overall=submit_status,
            ),
        }
        if job is not None:
            _upsert_cai_replace(job, row)
            _log(
                job,
                f"{site.upper()}: CAI VM replacement "
                + ("submitted" if result.get("ok") else "failed")
                + f" for {saved_id} → {target_id} ({', '.join(vms)}).",
            )
        if result.get("ok"):
            submitted.append(row)
        else:
            errors.append(f"{site.upper()} {saved_id}: {result.get('message') or 'replace failed'}")
    if job is not None:
        _persist_job(job)
    if not submitted and errors:
        raise HTTPException(400, " ".join(errors[:4]))
    payload: dict[str, Any] = {
        "ok": True,
        "submitted": submitted,
        "errors": errors,
        "job": _public_job(job) if job is not None else None,
        "savedIds": _saved_id_summary(job),
    }
    return payload


@app.post("/api/cai/integrate")
def api_cai_integrate(body: CaiIntegratePayload) -> dict[str, Any]:
    cookie = _require_cai_cookie()
    dests = cai_integrate_dests(body.dcs)
    if not body.items:
        raise HTTPException(400, "Select at least one saved content row.")
    if not dests:
        raise HTTPException(400, "Select at least one destination DC to integrate.")
    job = _maybe_job(body.job_id)
    submitted: list[dict[str, Any]] = []
    errors: list[str] = []
    for item in body.items:
        site = (item.site or "").strip().lower()
        saved_id = str(item.saved_id or "").strip()
        if not site or not saved_id:
            continue
        result = _integrate_saved_demo(
            cookie,
            site=site,
            saved_id=saved_id,
            dests=dests,
            job=job,
            wait_for_dests=False,
        )
        if result.get("loggedIn") is False:
            _set_cai_cookie("", result.get("message") or "")
            raise HTTPException(401, result.get("message") or "CAI session expired.")
        if result.get("ok"):
            submitted.append(result.get("row") or {"site": site, "savedId": saved_id})
        else:
            errors.append(f"{site.upper()} {saved_id}: {result.get('message') or 'integrate failed'}")
    if job is not None:
        _persist_job(job)
    if not submitted and errors:
        raise HTTPException(400, " ".join(errors[:4]))
    return {
        "ok": True,
        "submitted": submitted,
        "errors": errors,
        "job": _public_job(job) if job is not None else None,
        "savedIds": _saved_id_summary(job),
    }


def _template_key(source_path: str, server: str) -> str:
    return f"{str(source_path or '').strip().lower()}|{str(server or '').strip().lower()}"


def _upsert_cai_template(item: dict[str, Any]) -> dict[str, Any]:
    """Remember a submitted CAI Template so the Hub can show its status like a transfer."""
    key = _template_key(item.get("sourcePath", ""), item.get("server", ""))
    state = _managed_saved_state()
    rows = [row for row in (state.get("templates") or []) if isinstance(row, dict)]
    merged = dict(item)
    for existing in rows:
        if _template_key(existing.get("sourcePath", ""), existing.get("server", "")) == key:
            combined = dict(existing)
            combined.update(merged)
            merged = combined
            break
    rows = [
        row
        for row in rows
        if _template_key(row.get("sourcePath", ""), row.get("server", "")) != key
    ]
    rows.insert(0, merged)
    state["templates"] = rows[:40]
    _persist_managed_saved_state(state)
    return merged


def _cai_template_summary() -> list[dict[str, Any]]:
    rows = []
    for row in _managed_saved_state().get("templates") or []:
        if isinstance(row, dict) and row.get("server"):
            rows.append(row)
    return rows


_TEMPLATE_IN_FLIGHT_STATUSES = {"queuing", "processing", "waiting", "submitted"}


def _template_vm_from_task(task: dict[str, str]) -> str:
    """CAI shows a template's VM as `None || tc-clone-test3`; keep the VM name."""
    text = str(task.get("server") or "").strip()
    if "||" in text:
        text = text.rsplit("||", 1)[-1]
    return text.strip()


def _adopt_running_cai_templates(tasks: list[dict[str, str]], rows: list[dict[str, Any]]) -> bool:
    """Add template tasks running in CAI that this tool did not submit. Finished ones are skipped."""
    known = {str(row.get("server") or "").strip().lower() for row in rows}
    found: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if "template" not in str(task.get("type") or "").lower():
            continue
        vm = _template_vm_from_task(task)
        key = vm.lower()
        if not vm or key in known:
            continue
        status = normalize_task_status(str(task.get("status") or ""))
        _source, dest = parse_cai_task_dc(str(task.get("dc") or ""))
        if not dest:
            continue
        entry = found.setdefault(
            key,
            {
                "sourcePath": "",
                "server": vm,
                "dcs": [],
                "adopted": True,
                "submittedAt": str(task.get("updated") or "").strip(),
                "dcTasks": [],
                "_running": False,
            },
        )
        if dest not in entry["dcs"]:
            entry["dcs"].append(dest)
            entry["dcTasks"].append(
                {
                    "dc": dest,
                    "label": cai_dc_label(dest),
                    "status": status,
                    "newId": str(task.get("newId") or "").strip(),
                    "updated": str(task.get("updated") or "").strip(),
                }
            )
        if status in _TEMPLATE_IN_FLIGHT_STATUSES:
            entry["_running"] = True
    added = False
    for entry in found.values():
        if not entry.pop("_running", False):
            continue
        _upsert_cai_template(entry)
        added = True
    return added


def _refresh_cai_template_statuses(cookie: str) -> dict[str, Any]:
    rows = _cai_template_summary()
    listed = list_cai_tasks(cookie)
    if not listed.get("ok"):
        if listed.get("loggedIn") is False:
            _set_cai_cookie("", listed.get("message") or "")
        return listed
    tasks = listed.get("tasks") or []
    changed = _adopt_running_cai_templates(tasks, rows)
    if changed:
        rows = _cai_template_summary()
    pending = [
        row
        for row in rows
        if not row.get("dcTasks")
        or any(
            str(chip.get("status") or "") not in {"completed", "error"}
            for chip in (row.get("dcTasks") or [])
        )
    ]
    for row in pending:
        dests = [str(dc).strip().lower() for dc in (row.get("dcs") or []) if str(dc).strip()]
        hits = match_template_tasks(tasks, vm_name=str(row.get("server") or ""), dests=dests)
        by_dest: dict[str, dict[str, str]] = {}
        for task in hits:
            _source, dest = parse_cai_task_dc(str(task.get("dc") or ""))
            if dest and dest not in by_dest:
                by_dest[dest] = task
        chips = []
        for dest in dests:
            task = by_dest.get(dest)
            status = normalize_task_status(str(task.get("status") or "")) if task else "queuing"
            chips.append(
                {
                    "dc": dest,
                    "label": cai_dc_label(dest),
                    "status": status,
                    "newId": str((task or {}).get("newId") or "").strip(),
                    "updated": str((task or {}).get("updated") or "").strip(),
                }
            )
        if chips != (row.get("dcTasks") or []):
            row["dcTasks"] = chips
            _upsert_cai_template(row)
            changed = True
    return {
        "ok": True,
        "loggedIn": True,
        "templates": _cai_template_summary(),
        "changed": changed,
    }


@app.post("/api/cai/template")
def api_cai_template(body: CaiTemplatePayload) -> dict[str, Any]:
    cookie = _require_cai_cookie()
    result = submit_template(
        cookie,
        body.source_path,
        server=body.server,
        dest_dcs=body.dcs,
    )
    if result.get("loggedIn") is False:
        _set_cai_cookie("", result.get("message") or "")
        raise HTTPException(401, result.get("message") or "CAI session expired.")
    if not result.get("ok"):
        raise HTTPException(400, result.get("message") or "CAI template request failed.")
    _mark_cai_reachable("CAI session is active — reachable on this Cisco network.", cookie)
    dests = [str(dc).strip().lower() for dc in (result.get("dcs") or []) if str(dc).strip()]
    stored = _upsert_cai_template(
        {
            "sourcePath": str(result.get("sourcePath") or ""),
            "server": str(result.get("server") or ""),
            "dcs": dests,
            "caiUrl": str(result.get("caiUrl") or ""),
            "submittedAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "dcTasks": [
                {"dc": dc, "label": cai_dc_label(dc), "status": "queuing", "newId": ""}
                for dc in dests
            ],
        }
    )
    result["template"] = stored
    result["templates"] = _cai_template_summary()
    return result


@app.get("/api/cai/templates")
def api_cai_templates() -> dict[str, Any]:
    return {"ok": True, "templates": _cai_template_summary()}


@app.post("/api/cai/templates/refresh")
def api_cai_templates_refresh() -> dict[str, Any]:
    cookie = _cai_cookie()
    result = _refresh_cai_template_statuses(cookie)
    if result.get("loggedIn") is False:
        raise HTTPException(401, result.get("message") or "CAI session expired.")
    if not result.get("ok"):
        raise HTTPException(400, result.get("message") or "Could not refresh CAI templates.")
    return result


@app.post("/api/cai/templates/clear")
def api_cai_templates_clear() -> dict[str, Any]:
    state = _managed_saved_state()
    state["templates"] = []
    _persist_managed_saved_state(state)
    return {"ok": True, "templates": []}


@app.post("/api/cai/replace/refresh")
def api_cai_replace_refresh(body: CaiRefreshPayload) -> dict[str, Any]:
    cookie = _require_cai_cookie()
    job: dict[str, Any] | None = None
    jid = str(body.job_id or "").strip()
    if jid:
        try:
            job = _job(jid)
        except HTTPException:
            job = None
    if job is not None:
        refreshed = _refresh_cai_replace_statuses(job)
        if refreshed.get("loggedIn") is False:
            raise HTTPException(401, refreshed.get("message") or "CAI session expired.")
        if not refreshed.get("ok"):
            raise HTTPException(400, refreshed.get("message") or "Could not refresh CAI tasks.")
    integ = _refresh_cai_integrate_statuses(job)
    if integ.get("loggedIn") is False:
        raise HTTPException(401, integ.get("message") or "CAI session expired.")
    if not integ.get("ok"):
        raise HTTPException(400, integ.get("message") or "Could not refresh CAI integrations.")
    burn_in_job = integ.get("burnInJob")
    if burn_in_job is not None:
        job = burn_in_job
    return {
        "ok": True,
        "job": _public_job(job) if job is not None else None,
        "savedIds": _saved_id_summary(job),
        "tasks": integ.get("tasks") or [],
        "burnInScheduled": int(integ.get("burnInScheduled") or 0),
    }


@app.post("/api/saved-ids/hide")
def api_saved_ids_hide(body: CaiHidePayload) -> dict[str, Any]:
    if not body.items:
        raise HTTPException(400, "Check at least one saved content row to remove from this list.")
    job = _maybe_job(body.job_id)
    added = _hide_saved_ids(job, body.items)
    if added and job is not None:
        _log(
            job,
            "Removed "
            + ", ".join(
                f"{str(item.site or '').upper()} {item.saved_id}"
                for item in body.items[:8]
            )
            + " from the saved content list (local only).",
        )
        _persist_job(job)
        _persist_saved_ids(job)
    return {
        "ok": True,
        "removed": added,
        "job": _public_job(job) if job is not None else None,
        "savedIds": _saved_id_summary(job),
    }


@app.get("/api/saved-ids")
def api_saved_ids(job_id: str = "") -> dict[str, Any]:
    job = _maybe_job(job_id)
    return {
        "ok": True,
        "savedIds": _saved_id_summary(job),
        "job": _public_job(job) if job is not None else None,
        "cai": _cai_public_status(),
        "camgr": _camgr_public_status(),
    }


@app.post("/api/saved-ids/add")
def api_saved_ids_add(body: SavedIdsAddPayload) -> dict[str, Any]:
    if not body.items:
        raise HTTPException(400, "Check at least one saved content row to add.")
    rows = []
    for item in body.items:
        site = (item.site or "").strip().lower()
        saved_id = str(item.saved_id or "").strip()
        if not site or not saved_id:
            continue
        details: dict[str, Any] | None = None
        token = _cached_user_access_token()
        if token:
            details = fetch_content(token, site, saved_id)
        published = str(item.published_id or item.parent_id or "").strip()
        if not published or published == saved_id:
            published = _ensure_published_id(site, saved_id, published)
        name = str(item.name or (details or {}).get("name") or "").strip()
        topology_uid = extract_content_topology_uid(details or {}, site)
        rows.append(
            {
                "site": site,
                "savedId": saved_id,
                "publishedId": published,
                "name": name,
                "parentId": published or str(item.parent_id or "").strip(),
                "contentViewUrl": (
                    tbv3_edit_url(topology_uid)
                    or edit_topology_url(site, saved_id, details)
                ),
                "publishedLookupDone": True,
            }
        )
    if not rows:
        raise HTTPException(400, "Check at least one saved content row to add.")
    added = _upsert_managed_saved_rows(rows)
    job = _maybe_job(body.job_id)
    if job is not None:
        _log(
            job,
            "Added "
            + ", ".join(f"{row['site'].upper()} {row['savedId']}" for row in rows[:8])
            + " to the saved content list.",
        )
        _persist_job(job)
        _persist_saved_ids(job)
    return {
        "ok": True,
        "added": added,
        "job": _public_job(job) if job is not None else None,
        "savedIds": _saved_id_summary(job),
    }


@app.post("/api/saved-ids/lookup-parent")
def api_saved_ids_lookup_parent(body: SavedIdsLookupPayload) -> dict[str, Any]:
    site = (body.site or "").strip().lower()
    saved_id = str(body.saved_id or "").strip()
    if not site or not saved_id:
        raise HTTPException(400, "Saved content ID is required.")
    parent = _lookup_published_id(site, saved_id)
    _upsert_managed_saved_rows(
        [{
            "site": site,
            "savedId": saved_id,
            "publishedId": parent,
            "parentId": parent,
            "publishedLookupDone": True,
        }]
    )
    job = _maybe_job(body.job_id)
    return {
        "ok": True,
        "publishedId": parent,
        "job": _public_job(job) if job is not None else None,
        "savedIds": _saved_id_summary(job),
    }


@app.get("/api/camgr/status")
def api_camgr_status() -> dict[str, Any]:
    # Auto-connect handles the probe and falls back to the Chrome cookie on its own.
    return _camgr_auto_connect()


@app.post("/api/camgr/connect")
def api_camgr_connect() -> dict[str, Any]:
    return _connect_camgr(_camgr_cookie())


@app.post("/api/camgr/open")
def api_camgr_open() -> dict[str, Any]:
    started = _start_camgr_open()
    status = _camgr_public_status()
    status.update(started)
    return status


@app.post("/api/camgr/import-chrome")
def api_camgr_import_chrome() -> dict[str, Any]:
    cookie, _message = import_camgr_cookies_from_chrome()
    return _connect_camgr(cookie or "")


@app.post("/api/camgr/import-cookie")
def api_camgr_import_cookie(body: CamgrCookiePayload) -> dict[str, Any]:
    cookie = normalize_cookie_header(body.cookie)
    if cookie_header_is_tracking_only(cookie) or not cookie or "=" not in cookie:
        return _connect_camgr(_camgr_cookie())
    return _connect_camgr(cookie)


@app.get("/api/camgr/servers")
def api_camgr_servers() -> dict[str, Any]:
    cookie = _require_camgr_cookie()
    result = list_camgr_servers(cookie)
    if result.get("loggedIn") is False:
        _camgr_mark_unverified(result.get("message") or "")
        raise HTTPException(401, result.get("message") or "CAMGR session expired.")
    if not result.get("ok"):
        raise HTTPException(400, result.get("message") or "Could not load CAMGR datacenters.")
    return result


@app.post("/api/camgr/vms")
def api_camgr_vms(body: CaiVmsPayload) -> dict[str, Any]:
    cookie = _require_camgr_cookie()
    refs: list[CaiDemoRef] = list(body.items or [])
    if body.site and body.saved_id:
        first = CaiDemoRef(site=body.site, saved_id=body.saved_id)
        if not any(
            str(item.site).lower() == first.site.lower() and str(item.saved_id) == first.saved_id
            for item in refs
        ):
            refs.insert(0, first)
    if not refs:
        raise HTTPException(400, "Select at least one saved content row.")
    vms: list[dict[str, Any]] = []
    seen: set[str] = set()
    last_ok: dict[str, Any] | None = None
    last_err = "Could not load VMs from CAMGR."
    for ref in refs:
        result = fetch_camgr_vms(cookie, ref.site, ref.saved_id)
        if result.get("loggedIn") is False:
            _camgr_mark_unverified(result.get("message") or "")
            raise HTTPException(401, result.get("message") or "CAMGR session expired.")
        if not result.get("ok"):
            last_err = str(result.get("message") or last_err)
            continue
        last_ok = result
        site = str(result.get("site") or ref.site).strip().lower()
        saved_id = str(result.get("savedId") or ref.saved_id).strip()
        for vm in result.get("vms") or []:
            name = str(vm.get("name") or "").strip()
            parent = vm.get("parentServerId")
            key = f"{site}:{saved_id}:{parent}:{name}"
            if key in seen:
                continue
            seen.add(key)
            vms.append(
                {
                    "name": name,
                    "parentServerId": parent,
                    "os": vm.get("os") or "",
                    "site": site,
                    "savedId": saved_id,
                }
            )
    if last_ok is None:
        raise HTTPException(400, last_err)
    servers = list_camgr_servers(cookie)
    if servers.get("loggedIn") is False:
        _camgr_mark_unverified(servers.get("message") or "")
        raise HTTPException(401, servers.get("message") or "CAMGR session expired.")
    source_guids = sorted(
        {
            site_to_camgr_guid(str(ref.site or "").strip().lower()).upper()
            for ref in refs
            if str(ref.site or "").strip()
        }
    )
    return {
        "ok": True,
        "loggedIn": True,
        "vms": vms,
        "servers": servers.get("servers") or [],
        "title": last_ok.get("name") or "CAMGR VMs loaded.",
        "camgrDc": last_ok.get("camgrDc") or "",
        "camgrDcs": source_guids,
    }


@app.post("/api/camgr/vpod-vms")
def api_camgr_vpod_vms(body: CamgrVpodVmsPayload) -> dict[str, Any]:
    cookie = _require_camgr_cookie()
    result = fetch_camgr_vpod_vms(cookie, body.guid, body.vpod)
    if result.get("loggedIn") is False:
        _camgr_mark_unverified(result.get("message") or "")
        raise HTTPException(401, result.get("message") or "CAMGR session expired.")
    if not result.get("ok"):
        raise HTTPException(400, result.get("message") or "Could not load vPod VMs.")
    servers = list_camgr_servers(cookie)
    if servers.get("loggedIn") is False:
        _camgr_mark_unverified(servers.get("message") or "")
        raise HTTPException(401, servers.get("message") or "CAMGR session expired.")
    result["servers"] = servers.get("servers") or []
    result["homeDc"] = camgr_cdev_home_dc(body.guid)
    return result


@app.post("/api/camgr/vpod-transfer")
def api_camgr_vpod_transfer(body: CamgrVpodTransferPayload) -> dict[str, Any]:
    cookie = _require_camgr_cookie()
    guid = str(body.guid or "").strip()
    vpod = str(body.vpod or "").strip()
    dests = [str(dc).strip() for dc in body.dcs if str(dc).strip()]
    if not guid or not vpod:
        raise HTTPException(400, "Pick a ContentDEV datacenter and a vPod.")
    if not dests:
        raise HTTPException(400, "Select at least one destination DC.")
    discovered = fetch_camgr_vpod_vms(cookie, guid, vpod)
    if discovered.get("loggedIn") is False:
        _camgr_mark_unverified(discovered.get("message") or "")
        raise HTTPException(401, discovered.get("message") or "CAMGR session expired.")
    if not discovered.get("ok"):
        raise HTTPException(400, discovered.get("message") or "Could not load vPod VMs.")
    vms = discovered.get("vms") or []
    wanted = {str(name).strip().lower() for name in body.vm_names if str(name).strip()}
    chosen = [vm for vm in vms if not wanted or str(vm.get("name") or "").strip().lower() in wanted]
    servers = [vm["parentServerId"] for vm in chosen if vm.get("parentServerId") is not None]
    if not servers:
        raise HTTPException(400, f"No matching VMs in vPod {vpod} to transfer.")
    result = submit_camgr_vpod_transfer(
        cookie,
        source_guid=guid,
        vpod=vpod,
        servers=servers,
        dest_dcs=dests,
    )
    if result.get("loggedIn") is False:
        _camgr_mark_unverified(result.get("message") or "")
        raise HTTPException(401, result.get("message") or "CAMGR session expired.")
    if not result.get("ok"):
        raise HTTPException(400, result.get("message") or "CAMGR transfer failed.")
    names_out = [str(vm.get("name") or "") for vm in chosen]
    result["vmNames"] = names_out
    # A dev transfer has no saved demo, so it is tracked under its ContentDEV
    # guid and vPod. The UI already renders those rows as vPod-only.
    job = _maybe_job(body.job_id)
    camgr_job = result.get("job") or {}
    home = camgr_cdev_home_dc(guid)
    label = str(discovered.get("label") or "").strip() or f"vPod {vpod}"
    row = {
        "site": guid.lower(),
        "savedId": vpod,
        "demoId": str(result.get("demoId") or vpod),
        "camgrDc": str(result.get("camgrDc") or guid.upper()),
        "servers": result.get("servers") or servers,
        "vmNames": names_out,
        "dcs": result.get("dcs") or dests,
        "guid": str(result.get("guid") or camgr_job.get("guid") or ""),
        "status": str(result.get("status") or camgr_job.get("status") or "ready"),
        "statusRaw": str(result.get("statusRaw") or camgr_job.get("statusRaw") or "READY"),
        "progress": result.get("progress") or camgr_job.get("progress") or 0,
        "sessionId": camgr_job.get("sessionId") or 0,
        "owner": "",
        "message": result.get("message") or "",
        "integrate": False,
        "autoIntegrate": False,
        "autoIntegrateDcs": [],
        "autoIntegrateStatus": "",
        "autoIntegrateAttempts": 0,
    }
    _upsert_camgr_transfer(job, row)
    _upsert_managed_saved_rows(
        [
            {
                "site": guid.lower(),
                "savedId": vpod,
                "name": f"{guid.upper()} {label}".strip(),
            }
        ]
    )
    if job is not None:
        _log(
            job,
            f"{guid.upper()}: CAMGR dev transfer submitted for {label} → {home or ', '.join(dests)} "
            f"({', '.join(name for name in names_out if name)}).",
        )
        _persist_job(job)
    result["savedIds"] = _saved_id_summary(job)
    return result


@app.post("/api/camgr/transfer")
def api_camgr_transfer(body: CamgrTransferPayload) -> dict[str, Any]:
    cookie = _require_camgr_cookie()
    names = [str(name).strip() for name in body.vm_names if str(name).strip()]
    dests = [str(dc).strip() for dc in body.dcs if str(dc).strip()]
    auto_dests = cai_integrate_dests(dests) if body.auto_integrate else []
    if body.auto_burn_in and not body.auto_integrate:
        raise HTTPException(400, "Post-integration burn-in requires auto-integrate.")
    if not body.items:
        raise HTTPException(400, "Select at least one saved content row.")
    if not dests:
        raise HTTPException(400, "Select at least one destination DC.")
    missing_vpod = [
        dc for dc in dests if is_cdev_camgr_guid(dc) and not dc.rsplit(":", 1)[-1].isdigit()
    ]
    if missing_vpod:
        raise HTTPException(
            400,
            f"Pick a vPod for {', '.join(missing_vpod)}. "
            "ContentDEV transfers land in a vPod, so CAMGR needs the vPod number.",
        )
    source_guids = {
        site_to_camgr_guid((item.site or "").strip().lower()).upper()
        for item in body.items
        if (item.site or "").strip()
    }
    for dest in dests:
        home = camgr_cdev_home_dc(dest)
        stray = sorted(guid for guid in source_guids if home and guid != home)
        if stray:
            raise HTTPException(
                400,
                f"{dest.split(':', 1)[0]} only takes content from {home}, "
                f"so it cannot be a destination for {', '.join(stray)}.",
            )
    if body.auto_integrate and not auto_dests:
        raise HTTPException(
            400,
            "Auto-integrate needs at least one CAI dest DC (RTP, SJC, EMEA, APJ, or SYD). "
            "ContentDEV is not an Integrate destination.",
        )
    job = _maybe_job(body.job_id)
    submitted: list[dict[str, Any]] = []
    errors: list[str] = []
    for item in body.items:
        site = (item.site or "").strip().lower()
        saved_id = str(item.saved_id or "").strip()
        if not site or not saved_id:
            continue
        discovered = fetch_camgr_vms(cookie, site, saved_id)
        if discovered.get("loggedIn") is False:
            _camgr_mark_unverified(discovered.get("message") or "")
            raise HTTPException(401, discovered.get("message") or "CAMGR session expired.")
        if not discovered.get("ok"):
            errors.append(f"{site.upper()} {saved_id}: {discovered.get('message') or 'could not load VMs'}")
            continue
        vms = discovered.get("vms") or []
        if names:
            wanted = {name.lower() for name in names}
            servers = [
                int(vm["parentServerId"])
                for vm in vms
                if str(vm.get("name") or "").strip().lower() in wanted
                and vm.get("parentServerId") is not None
            ]
        else:
            servers = [int(vm["parentServerId"]) for vm in vms if vm.get("parentServerId") is not None]
        if not servers:
            errors.append(f"{site.upper()} {saved_id}: no matching VMs to transfer.")
            continue
        result = submit_camgr_transfer(
            cookie,
            site=site,
            saved_id=saved_id,
            servers=servers,
            dest_dcs=dests,
            integrate=bool(body.integrate),
            integrate_name=body.integrate_name,
            integrate_wait=bool(body.integrate_wait),
            source_dc=str(discovered.get("camgrDc") or site_to_camgr_guid(site)),
        )
        if result.get("loggedIn") is False:
            _camgr_mark_unverified(result.get("message") or "")
            raise HTTPException(401, result.get("message") or "CAMGR session expired.")
        camgr_job = result.get("job") or {}
        row = {
            "site": site,
            "savedId": saved_id,
            "demoId": str(result.get("demoId") or saved_id),
            "camgrDc": str(result.get("camgrDc") or site_to_camgr_guid(site)),
            "servers": result.get("servers") or servers,
            "vmNames": names,
            "dcs": result.get("dcs") or dests,
            "guid": str(result.get("guid") or camgr_job.get("guid") or ""),
            "status": str(result.get("status") or camgr_job.get("status") or "error"),
            "statusRaw": str(result.get("statusRaw") or camgr_job.get("statusRaw") or ""),
            "progress": result.get("progress") or camgr_job.get("progress") or 0,
            "sessionId": camgr_job.get("sessionId") or 0,
            "owner": str(discovered.get("owner") or ""),
            "message": result.get("message") or "",
            "integrate": bool(body.integrate),
            "autoIntegrate": bool(body.auto_integrate),
            "autoIntegrateDcs": auto_dests,
            "autoIntegrateStatus": "pending" if body.auto_integrate else "",
            "autoIntegrateAttempts": 0,
            "autoBurnIn": bool(body.auto_burn_in),
            "burnInDays": max(1, int(body.burn_in_days or 1)),
            "burnInStatus": "pending" if body.auto_burn_in else "",
            "burnInJobId": "",
        }
        _upsert_camgr_transfer(job, row)
        if job is not None:
            _log(
                job,
                f"{site.upper()}: CAMGR transfer "
                + ("submitted" if result.get("ok") else "failed")
                + f" for {saved_id} → {', '.join(dests)}"
                + (
                    f", then CAI Integrate to {', '.join(cai_dc_label(dc) for dc in auto_dests)}"
                    if body.auto_integrate
                    else ""
                )
                + (
                    f", then schedule {len(auto_dests)} regular burn-in session(s) "
                    f"for {max(1, int(body.burn_in_days or 1))} day(s)"
                    if body.auto_burn_in
                    else ""
                )
                + ".",
            )
        if result.get("ok"):
            submitted.append(row)
        else:
            errors.append(f"{site.upper()} {saved_id}: {result.get('message') or 'transfer failed'}")
    if job is not None:
        _persist_job(job)
    if not submitted and errors:
        raise HTTPException(400, " ".join(errors[:4]))
    return {
        "ok": True,
        "submitted": submitted,
        "errors": errors,
        "job": _public_job(job) if job is not None else None,
        "savedIds": _saved_id_summary(job),
    }


@app.post("/api/camgr/transfer/refresh")
def api_camgr_transfer_refresh(body: CaiRefreshPayload) -> dict[str, Any]:
    job = _maybe_job(body.job_id)
    refreshed = _refresh_camgr_transfer_statuses(job)
    if refreshed.get("loggedIn") is False:
        raise HTTPException(401, refreshed.get("message") or "CAMGR session expired.")
    if not refreshed.get("ok"):
        raise HTTPException(400, refreshed.get("message") or "Could not refresh CAMGR transfers.")
    return {
        "ok": True,
        "job": _public_job(job) if job is not None else None,
        "savedIds": _saved_id_summary(job),
        "autoIntegrated": refreshed.get("autoIntegrated") or [],
    }


@app.post("/api/camgr/jobs/import")
def api_camgr_jobs_import(body: CamgrImportJobsPayload) -> dict[str, Any]:
    job = _maybe_job(body.job_id)
    imported = _import_in_progress_camgr_jobs(job, body.items)
    if imported.get("loggedIn") is False:
        raise HTTPException(400, imported.get("message") or "CAMGR session expired.")
    if not imported.get("ok"):
        raise HTTPException(400, imported.get("message") or "Could not list CAMGR transfers.")
    return {
        "ok": True,
        "imported": imported.get("imported") or [],
        "found": imported.get("found") or 0,
        "job": _public_job(job) if job is not None else None,
        "savedIds": _saved_id_summary(job),
        "camgr": _camgr_public_status(),
    }


@app.post("/api/share/users/search")
def api_share_users_search(body: ShareSearchPayload) -> dict[str, Any]:
    token = _resolve_token(body)
    site = (body.site or "").strip().lower()
    if site not in SITES:
        raise HTTPException(400, "Datacenter must be SJC, RTP, LON, SNG, or SYD.")
    kind = _share_kind(body)
    users, err = search_share_users(
        token,
        site,
        body.query or "",
        content_scope=(kind == "content"),
    )
    if err:
        raise HTTPException(400, err)
    return {"ok": True, "users": users}


@app.post("/api/share/state")
def api_share_state(body: ShareStatePayload) -> dict[str, Any]:
    token = _resolve_token(body)
    site = (body.site or "").strip().lower()
    if site not in SITES:
        raise HTTPException(400, "Datacenter must be SJC, RTP, LON, SNG, or SYD.")
    kind = _share_kind(body)
    if kind == "content":
        content_id = str(body.content_id or "").strip()
        if not content_id:
            raise HTTPException(400, "Content ID is required.")
        shared_with, err = fetch_content_shared_with(token, site, content_id)
    else:
        session_id = str(body.session_id or "").strip()
        if not session_id:
            raise HTTPException(400, "Session ID is required.")
        shared_with, err = fetch_session_shared_with(token, site, session_id)
    if err:
        raise HTTPException(400, err)
    return {"ok": True, "sharedWith": shared_with, "kind": kind}


@app.post("/api/share/update")
def api_share_update(body: ShareUpdatePayload) -> dict[str, Any]:
    token = _resolve_token(body)
    site = (body.site or "").strip().lower()
    if site not in SITES:
        raise HTTPException(400, "Datacenter must be SJC, RTP, LON, SNG, or SYD.")
    kind = _share_kind(body)
    payload = [row.model_dump() for row in body.shared_with]
    if kind == "content":
        content_id = str(body.content_id or "").strip()
        if not content_id:
            raise HTTPException(400, "Content ID is required.")
        ok, err = update_content_share(token, site, content_id, payload)
    else:
        session_id = str(body.session_id or "").strip()
        if not session_id:
            raise HTTPException(400, "Session ID is required.")
        ok, err = update_session_share(token, site, session_id, payload)
    if not ok:
        raise HTTPException(400, err or "Share update failed.")
    shared_with = [
        {"userId": str(row.get("userId") or "").strip(), "fullName": str(row.get("fullName") or "").strip()}
        for row in payload
        if str(row.get("userId") or "").strip()
    ]
    job_out: dict[str, Any] | None = None
    job_id = str(body.job_id or "").strip()
    if job_id:
        job = _job(job_id)
        _apply_share_to_job_dc(
            job,
            site=site,
            kind=kind,
            session_id=str(body.session_id or ""),
            content_id=str(body.content_id or ""),
            shared_with=shared_with,
        )
        _persist_job(job)
        job_out = _public_job(job)
    return {"ok": True, "sharedWith": shared_with, "kind": kind, "job": job_out}


@app.post("/api/contents/mine")
def api_my_contents(body: TokenPayload) -> dict[str, Any]:
    token = _resolve_token(body)
    result = list_saved_contents_all_sites(token)
    return {"ok": True, **result}


@app.post("/api/surveys/decline")
def api_decline_surveys(body: DeclineSurveysPayload) -> dict[str, Any]:
    token = _resolve_token(body)
    items: list[tuple[str, str]] = []
    if body.decline_all:
        listed = list_pending_surveys_all_sites(token)
        items = [
            (str(row.get("site") or "").lower(), str(row.get("surveyId") or "").strip())
            for row in listed.get("surveys") or []
            if row.get("site") and row.get("surveyId")
        ]
    else:
        for item in body.items:
            site = (item.site or "").strip().lower()
            survey_id = str(item.survey_id or "").strip()
            if site in SITES and survey_id:
                items.append((site, survey_id))
    if not items:
        return {"ok": True, "declined": 0, "failed": 0, "results": []}
    result = decline_surveys(token, items)
    return {"ok": result["ok"], **result}


@app.post("/api/contents/delete")
def api_delete_contents(body: DeleteContentsPayload) -> dict[str, Any]:
    token = _resolve_token(body)
    items = [
        ((item.site or "").strip().lower(), str(item.content_id or "").strip())
        for item in body.items
    ]
    items = [(site, cid) for site, cid in items if site in SITES and cid]
    if not items:
        raise HTTPException(400, "Select at least one saved content item to delete.")
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(len(items), 10)) as pool:
        futures = [
            pool.submit(delete_saved_content, token, site, content_id)
            for site, content_id in items
        ]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"ok": False, "message": str(exc)})
    ok = all(item.get("ok") for item in results)
    deleted = sum(1 for item in results if item.get("ok"))
    hide_items = [
        CaiDemoRef(site=str(row.get("site") or ""), saved_id=str(row.get("contentId") or ""))
        for row in results
        if row.get("ok") and str(row.get("site") or "").strip() and str(row.get("contentId") or "").strip()
    ]
    job = _maybe_job(body.job_id)
    if hide_items:
        _hide_saved_ids(job, hide_items)
        if job is not None:
            _log(
                job,
                "Removed "
                + ", ".join(
                    f"{str(item.site or '').upper()} {item.saved_id}"
                    for item in hide_items[:8]
                )
                + " from the Content Automation Hub list after Cleanup delete.",
            )
            _persist_job(job)
            _persist_saved_ids(job)
    return {
        "ok": ok,
        "deleted": deleted,
        "failed": len(results) - deleted,
        "results": results,
        "job": _public_job(job) if job is not None else None,
        "savedIds": _saved_id_summary(job),
    }


@app.post("/api/jobs/schedule-saved")
def api_schedule_saved(body: ScheduleSavedPayload) -> dict[str, Any]:
    return _schedule_saved_job(body)


@app.post("/api/jobs/schedule-pending")
def api_schedule_pending(body: ScheduleSavedPayload) -> dict[str, Any]:
    """Schedule any cards still waiting (queued/scheduling) without adding new ones."""
    job_id = str(body.job_id or "").strip()
    if not job_id:
        raise HTTPException(400, "job_id required")
    job = _job(job_id)
    pending_count = sum(1 for dc in job.get("dcs") or [] if _dc_needs_schedule(dc))
    if not pending_count:
        return _public_job(job)
    if job.get("_schedule_in_progress") or job.get("worker_alive"):
        _log(job, "Schedule already running — skipping duplicate schedule-pending request.")
        return _public_job(job)
    token = _resolve_token(body)
    job["token"] = token
    job["token_source"] = body.dcloud_token_source
    job["token_at"] = time.time()
    if body.selected_vms:
        job["selected_vms"] = [vm.model_dump() for vm in body.selected_vms]
    elif not job.get("selected_vms"):
        raise HTTPException(400, "Select VMs in Step 2, or restore a job that already has them.")
    run_payload = RunPayload(
        dcloud_token=body.dcloud_token,
        dcloud_token_source=body.dcloud_token_source,
        demo_ids=DemoIds(),
        selected_vms=[SelectedVm(**vm) for vm in job["selected_vms"]],
        days=body.days,
        start_at=body.start_at,
        stop_at=body.stop_at,
        active_timeout_minutes=body.active_timeout_minutes,
        content_export=body.content_export,
        auto_next_available=body.auto_next_available,
    )
    _log(job, f"Scheduling {pending_count} pending session(s) in parallel.")
    threading.Thread(
        target=_schedule_and_watch_new_dcs,
        args=(job, run_payload),
        daemon=True,
    ).start()
    _persist_job(job)
    return _public_job(job)


@app.post("/api/session")
def api_session(body: LoadVmsPayload) -> dict[str, Any]:
    token = _resolve_token(body)
    site, session_id = parse_site_and_id(body.session_id, body.site)
    details, err = fetch_session(token, site, session_id, expand="server")
    if err or not details:
        raise HTTPException(400, err or "Not found.")
    return {"ok": True, "session": details}


_load_last_job()

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8768"))
    bundled_auth = APP_DIR / "browser_auth"
    auth_dir = bundled_auth if bundled_auth.is_dir() else (_SCRIPTING_ROOT / "browser_auth")
    reload_dirs = [str(APP_DIR), str(auth_dir)]
    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=port,
        reload=True,
        reload_dirs=reload_dirs,
    )
