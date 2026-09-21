"""dCloud session APIs for multi-DC scheduling and session management."""

from __future__ import annotations

import base64
import binascii
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any, Callable
from urllib.parse import quote, urlencode, urljoin, urlparse

import requests
import urllib3

from net_errors import describe_request_error, looks_off_network
from browser_auth.dcloud_token import (
    DEFAULT_TIMEOUT,
    dcloud_auth_header,
    normalize_dcloud_token,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SITES = ("sjc", "rtp", "lon", "sng", "syd")
KNOWN_SITES = frozenset(SITES)
_ADMIN_SEARCH_CACHE_SECONDS = 15 * 60
_admin_search_cache_lock = threading.Lock()
_admin_search_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}

# Same Dev Pool IDs the dCloud bot /sch command uses.
DEV_POOLS = {
    "rtp": "dbtrlj8qg2ey6nfvw6ooeaqks",
    "sjc": "dwy2fhw8w8rxdlzl0wwa3xj58",
    "lon": "2bstymrd7q03jygha7glua2lo",
    "sng": "8oaulypgb7540ffe81epjd17b",
}
CORE_POOL = "core-content-pool"

# Bot /sch ID,days,min,exp sets contentExport to the JSON string "true".
# Regular /sch ID,days,min uses "false".
CONTENT_EXPORT = "true"
CONTENT_REGULAR = "false"

# Authenticated session GET uses numeric codes (2 = starting, 4 = Active).
# Public /api/public/checkSession returns names: ACTIVE, STARTING_UP, …
ACTIVE_STATUSES = {"active", "available", "4"}
ACTIVE_NUMERIC = {4}
# Numeric codes seen alongside the public names: 10 = PRESERVE and 12 = SAVING are
# still in progress, 13 = SAVED is the last status dCloud reports for a save.
SAVING_IN_PROGRESS_STATUSES = {"saving", "preserve", "preserving", "10", "12"}
SAVING_IN_PROGRESS_NUMERIC = {10, 12}
SAVED_STATUSES = {"saved", "13"}
SAVED_NUMERIC = {13}
FAILED_STATUSES = {
    "failed",
    "error",
    "cancelled",
    "canceled",
    "ended",
    "notfound",
    "not found",
    "complete",
    "deleted",
    "sessiondelete",
    "shutting_down",
    "shuttingdown",
}
STOPPING_STATUSES = {"stopping", "5"}
STOPPING_NUMERIC = {5}
# Words dCloud shows for the numeric session codes. Unlisted codes stay numeric
# rather than guessing a label.
SESSION_STATUS_LABELS = {
    "1": "Scheduled",
    "2": "Starting",
    "4": "Active",
    "5": "Stopping",
    "7": "Cancelled",
    "9": "Deleted",
    "10": "Preserve",
    "12": "Saving",
    "13": "Saved",
    "95": "VC Unavailable",
    "99": "Error",
}
POWER_ON_STATES = {"poweredon", "powered_on", "power_on", "poweron", "on", "running"}
POWER_OFF_STATES = {"poweredoff", "powered_off", "power_off", "poweroff", "off", "notrunning"}

TBV3_API = "https://tbv3-production.ciscodcloud.com"
TBV3_UI = "https://tbv3-ui.ciscodcloud.com"


def tbv3_edit_url(topology_uid: str) -> str:
    uid = str(topology_uid or "").strip()
    if not uid or uid.isdigit():
        return ""
    return f"{TBV3_UI}/edit/{uid}"
DEFAULT_SAVE_DESCRIPTION = "Saved"
_HTML_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

Progress = Callable[[str], None]
GetToken = Callable[[], str]
RefreshAuth = Callable[[str], tuple[str, str | None]]


def is_auth_error(err: Any) -> bool:
    text = str(err or "").lower()
    return "401" in text or "token was rejected" in text or "unauthorized" in text


def site_base(site: str) -> str:
    return f"https://dcloud2-{site}.cisco.com"


def edit_topology_url(
    site: str,
    content_id: str,
    content: dict[str, Any] | None = None,
) -> str:
    """v2 custom-content Edit Topology: /topology/builder/{id} (same rel as the dashboard)."""
    cid = str(content_id or "").strip()
    if not cid:
        return ""
    if isinstance(content, dict):
        links = content.get("links")
        if isinstance(links, list):
            for link in links:
                if not isinstance(link, dict):
                    continue
                rel = str(link.get("rel") or "").lower()
                href = str(link.get("href") or "").strip()
                if rel == "edittopology" and href:
                    if href.startswith("http://") or href.startswith("https://"):
                        return href
                    return site_base(site) + (href if href.startswith("/") else f"/{href}")
    return f"{site_base(site)}/topology/builder/{cid}"


def session_view_v2_url(site: str, session_id: str) -> str:
    site_code = (site or "").strip().lower()
    sid = (session_id or "").strip()
    if not site_code or not sid:
        return ""
    return (
        f"{site_base(site_code)}/session/{sid}"
        "?returnPathTitleKey=view-session"
    )


def session_view_v3_url(session_id: str, topology_version_uid: str) -> str:
    sid = (session_id or "").strip()
    uid = (topology_version_uid or "").strip()
    if not sid or not uid:
        return ""
    return (
        f"https://tbv3-ui.ciscodcloud.com/sessions/{sid}"
        f"?versionUid={uid}"
    )


def extract_topology_uid(session: dict[str, Any] | None) -> str:
    if not session:
        return ""
    for key in ("topologyVersionUid", "versionUid", "versionUID"):
        value = session.get(key)
        if value:
            return str(value).strip()
    nested = session.get("topologyVersion")
    if isinstance(nested, dict) and nested.get("uid"):
        return str(nested.get("uid") or "").strip()
    return ""


def session_view_url(
    site: str,
    session_id: str,
    topology_uid: str | None = None,
    session: dict[str, Any] | None = None,
) -> str:
    """tbv3 when topologyVersionUid is present (same as the bot /qsd View v3); else v2."""
    uid = (topology_uid or "").strip() or extract_topology_uid(session)
    if uid:
        v3 = session_view_v3_url(session_id, uid)
        if v3:
            return v3
    return session_view_v2_url(site, session_id)


def server_rdp_url(site: str, session_id: str, vm_uid: str) -> str:
    return f"{site_base(site)}/sessions/{session_id}/servers/{vm_uid}/rdp"


def vm_console_url(site: str, session_id: str, vm_uid: str) -> str:
    return f"{site_base(site)}/sessions/{session_id}/servers/{vm_uid}/console"


def webrdp_connect_url(site: str, session_id: str, vm_uid: str, credentials: str) -> str:
    return (
        f"http://dcloud-{site}-web-4.cisco.com/dCloudConnect"
        f"?s={vm_uid}&ss={session_id}&p=rdp#/client/{credentials}"
    )


def fetch_webrdp_credentials(token: str, site: str, session_id: str) -> str:
    """Session-level WebRDP cookie/credentials — same GET the bot /sd command uses."""
    url = f"{site_base(site)}/api/sessions/{session_id}/servers/{session_id}/webrdp"
    try:
        response = _request("GET", url, token, timeout=30)
    except requests.RequestException:
        return ""
    body = _json_or_text(response)
    if not isinstance(body, dict):
        return ""
    cookie = body.get("cookie") or {}
    value = cookie.get("value") if isinstance(cookie, dict) else ""
    if not value:
        return ""
    try:
        parsed = json.loads(value)
    except ValueError:
        return ""
    if isinstance(parsed, dict):
        return str(parsed.get("credentials") or "")
    return ""


def attach_vm_access_links(
    token: str,
    site: str,
    session_id: str,
    vms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    creds = fetch_webrdp_credentials(token, site, session_id)
    enriched: list[dict[str, Any]] = []
    for vm in vms:
        item = dict(vm)
        uid = str(item.get("uid") or "")
        item.pop("rdpUrl", None)
        item.pop("webRdpUrl", None)
        if uid:
            item["consoleUrl"] = vm_console_url(site, session_id, uid)
            if _flag_true(item.get("rdpEnabled")):
                item["rdpUrl"] = server_rdp_url(site, session_id, uid)
                if creds:
                    item["webRdpUrl"] = webrdp_connect_url(site, session_id, uid, creds)
        enriched.append(item)
    return enriched


def parse_site_and_id(raw: str, default_site: str = "") -> tuple[str, str]:
    text = (raw or "").strip()
    if not text:
        return (default_site or "").lower(), ""
    lower = text.lower()
    if len(lower) > 3 and lower[:3] in KNOWN_SITES:
        rest = text[3:].lstrip("-: /")
        if rest:
            return lower[:3], rest
    return (default_site or "").lower(), text


def _headers(token: str) -> dict[str, str]:
    return dcloud_auth_header(normalize_dcloud_token(token))


def _host_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host.endswith(".cisco.com") or host.endswith(".ciscodcloud.com") or host in {
        "cisco.com",
        "ciscodcloud.com",
    }


def _request(
    method: str,
    url: str,
    token: str,
    *,
    json_body: Any | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    extra_headers: dict[str, str] | None = None,
    include_auth: bool = True,
) -> requests.Response:
    """Follow redirects without turning POST/PUT into GET (that yields Tomcat 405 HTML)."""
    headers = _headers(token) if include_auth else {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if extra_headers:
        headers.update({key: value for key, value in extra_headers.items() if value})
    current_url = url
    current_method = (method or "GET").upper()
    current_json = json_body
    last: requests.Response | None = None
    for _ in range(5):
        try:
            last = requests.request(
                current_method,
                current_url,
                headers=headers,
                json=current_json,
                verify=False,
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            # Callers turn these into UI text, so say "get on the VPN" rather
            # than pasting urllib3's NameResolutionError at the user.
            if looks_off_network(exc):
                raise requests.ConnectionError(
                    describe_request_error(exc, "dCloud")
                ) from exc
            raise
        if last.status_code not in (301, 302, 303, 307, 308):
            return last
        location = last.headers.get("Location") or last.headers.get("location") or ""
        if not location:
            return last
        next_url = urljoin(current_url, location)
        if not _host_allowed(next_url):
            return last
        if last.status_code == 303:
            current_method = "GET"
            current_json = None
        current_url = next_url
    return last


def _json_or_text(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text.strip()


def _looks_like_html(text: str) -> bool:
    lowered = text.lstrip().lower()
    return lowered.startswith("<!doctype") or lowered.startswith("<html") or "<h1>" in lowered


def api_message(body: Any) -> str:
    if isinstance(body, str):
        if _looks_like_html(body):
            title = _HTML_TITLE_RE.search(body)
            if title:
                return re.sub(r"\s+", " ", title.group(1)).strip()
            return "HTML error page from dCloud"
        return body
    if isinstance(body, list) and body:
        first = body[0]
        if isinstance(first, dict):
            return api_message(first)
        return str(first)
    if not isinstance(body, dict):
        return str(body)
    message = body.get("message")
    if isinstance(message, list) and message:
        first = message[0]
        return first if isinstance(first, str) else str(first)
    if isinstance(message, str) and message:
        return message
    for key in ("developerMessage", "error", "detail", "status"):
        value = body.get(key)
        if value:
            return str(value)
    return ""


def _short_http_message(body: Any, status_code: int) -> str:
    message = api_message(body).strip()
    if not message:
        return f"HTTP {status_code}"
    if _looks_like_html(message) or len(message) > 180:
        return f"HTTP {status_code}"
    if str(status_code) in message:
        return message
    return f"HTTP {status_code}: {message}"


def parse_schedule_datetime(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def resolve_schedule_window(
    *,
    days: int = 1,
    start_at: str = "",
    stop_at: str = "",
) -> tuple[datetime, datetime] | str:
    # No day cap here. dCloud decides what window a demo allows and says so.
    days = max(1, int(days or 1))
    start = parse_schedule_datetime(start_at) or datetime.now(timezone.utc)
    stop = parse_schedule_datetime(stop_at)
    if stop is None:
        stop = start + timedelta(days=days)
    if stop <= start:
        return "Stop time must be after start time."
    return start, stop


def advance_past_schedule_window(
    start: datetime,
    stop: datetime,
    *,
    now: datetime | None = None,
    grace_seconds: int = 15,
) -> tuple[datetime, datetime, bool]:
    """If the chosen start is already in the past, slide the whole window forward.

    Delay is applied after this, so a stale 13:10 plus a 5 minute delay becomes
    now plus 5 minutes instead of a time dCloud treats as 'start immediately'.
    """
    current = now or datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if stop.tzinfo is None:
        stop = stop.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if start >= current - timedelta(seconds=max(0, int(grace_seconds or 0))):
        return start, stop, False
    shift = current - start
    return current, stop + shift, True


MAX_SCHEDULE_COPIES = 20
# dCloud itself holds back a second session of the same demo that starts at the
# same moment, so copies of one demo are never scheduled closer than this.
MIN_SAME_DEMO_GAP_MINUTES = 4


def schedule_copy_offsets_minutes(
    delay_minutes: int = 0,
    session_count: int = 1,
    target_count: int = 1,
) -> list[int]:
    """Minutes to add to the chosen start, one per session, in creation order.

    Every session after the first waits one more delay, whether it is another
    demo checked in the same click or another copy of the same one. A session
    scheduled on its own has nothing to follow, so the delay pushes out its own
    start instead.

    Sessions are created one round of demos at a time, so two copies of the same
    demo sit target_count apart. That spacing is widened to
    MIN_SAME_DEMO_GAP_MINUTES when the delay would stack them up.
    """
    delay = max(0, int(delay_minutes or 0))
    copies = max(1, min(int(session_count or 1), MAX_SCHEDULE_COPIES))
    targets = max(1, int(target_count or 1))
    total = copies * targets
    if total == 1:
        return [delay]
    round_gap = delay * targets
    bump = max(0, MIN_SAME_DEMO_GAP_MINUTES - round_gap) if copies > 1 else 0
    return [delay * index + bump * (index // targets) for index in range(total)]


def _dcloud_timestamp(when: datetime) -> str:
    utc = when.astimezone(timezone.utc).replace(tzinfo=None)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _status_text(raw: Any) -> str:
    if raw is None:
        return ""
    return str(raw).strip()


def _status_label(raw: Any) -> str:
    """The session and admin APIs report status as a bare number; show the word."""
    text = _status_text(raw)
    return SESSION_STATUS_LABELS.get(text, text)


def _status_key(raw: Any) -> str:
    return _status_text(raw).lower().replace(" ", "")


def is_active_status(raw: Any) -> bool:
    if isinstance(raw, bool):
        return False
    if isinstance(raw, int):
        return raw in ACTIVE_NUMERIC
    key = _status_key(raw)
    if key in ACTIVE_STATUSES:
        return True
    try:
        return int(key) in ACTIVE_NUMERIC
    except ValueError:
        return False


def is_failed_status(raw: Any) -> bool:
    key = _status_key(raw)
    return key in FAILED_STATUSES or key.startswith("fail")


def is_stopping_status(raw: Any) -> bool:
    if isinstance(raw, int):
        return raw in STOPPING_NUMERIC
    key = _status_key(raw)
    if key in STOPPING_STATUSES:
        return True
    try:
        return int(key) in STOPPING_NUMERIC
    except ValueError:
        return False


def is_saving_in_progress_status(raw: Any) -> bool:
    """True while dCloud is still writing the save (session is usually still listed)."""
    if isinstance(raw, int):
        return raw in SAVING_IN_PROGRESS_NUMERIC
    key = _status_key(raw)
    if key in SAVING_IN_PROGRESS_STATUSES:
        return True
    try:
        return int(key) in SAVING_IN_PROGRESS_NUMERIC
    except ValueError:
        return False


def is_saved_status(raw: Any) -> bool:
    """True when dCloud reports SAVED, its terminal status for a save."""
    if isinstance(raw, int):
        return raw in SAVED_NUMERIC
    key = _status_key(raw)
    if key in SAVED_STATUSES:
        return True
    try:
        return int(key) in SAVED_NUMERIC
    except ValueError:
        return False


def session_saved_content_id(session: dict[str, Any] | None) -> str:
    """Durable custom-content ID: v2 session.activeId / tbv3 sessionDetails.activeDemoId."""
    if not isinstance(session, dict):
        return ""
    nested = []
    for key in ("session", "sessionDetails"):
        value = session.get(key)
        if isinstance(value, dict):
            nested.append(value)
    for obj in (session, *nested):
        for key in ("activeId", "activeDemoId"):
            val = str(obj.get(key) or "").strip()
            if val and val.lower() not in {"none", "null"}:
                return val
    return ""


def format_status(*parts: Any) -> str:
    labels: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = _status_label(part)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        labels.append(text)
    return " / ".join(labels) if labels else "unknown"


def check_public_session_status(site: str, session_id: str) -> tuple[str, str | None]:
    """Named status from GET /api/public/checkSession (no auth), same as the cleanup scripts."""
    site_code = (site or "").strip().lower()
    sid = (session_id or "").strip()
    if site_code not in KNOWN_SITES or not sid:
        return "", "site and session_id required"
    url = f"{site_base(site_code)}/api/public/checkSession?sessionId={sid}"
    try:
        response = requests.get(url, verify=False, timeout=10)
    except requests.RequestException as exc:
        return "", str(exc)
    body: Any = _json_or_text(response)
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return body.strip(), None
    if isinstance(body, dict):
        return str(body.get("status") or "").strip(), None
    return "", f"HTTP {response.status_code}"


def _power_key(raw: Any) -> str:
    return str(raw or "").strip().lower().replace(" ", "").replace("-", "_")


def is_powered_on(raw: Any) -> bool:
    return _power_key(raw) in POWER_ON_STATES


def is_powered_off(raw: Any) -> bool:
    return _power_key(raw) in POWER_OFF_STATES


def _flag_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"true", "1", "yes", "on"}


def _vm_os_text(raw: dict[str, Any] | None) -> str:
    if not isinstance(raw, dict):
        return ""
    for key in (
        "os",
        "guestOs",
        "guestOS",
        "operatingSystem",
        "guestFullName",
        "serverOs",
        "serverOS",
        "templateName",
        "vmTemplateName",
        "platform",
        "serverTemplateName",
    ):
        text = str(raw.get(key) or "").strip()
        if text:
            return text
    for nest_key in ("vmwareState", "configuration", "config", "serverTemplate", "template", "vmTemplate"):
        nested = raw.get(nest_key)
        if isinstance(nested, dict):
            found = _vm_os_text(nested)
            if found:
                return found
    return ""


def _vm_short_name_from_raw(raw: dict[str, Any]) -> str:
    for key in ("shortName", "vmName", "hostname"):
        text = str(raw.get(key) or "").strip()
        if text:
            return text
    return str(raw.get("name") or "").strip()


def _vm_display_name_from_raw(raw: dict[str, Any], short_name: str = "") -> str:
    # Never use description here — Topology Builder keeps notes/IPs/creds in
    # Description, and the session card should show Name (Jumphost, rwkst2).
    preset = str(raw.get("displayName") or "").strip()
    if preset:
        return preset
    for key in ("contentName", "serverLabel", "label", "title"):
        text = str(raw.get(key) or "").strip()
        if text:
            return text
    return short_name or _vm_short_name_from_raw(raw)


def summarize_vm(vm: dict[str, Any]) -> dict[str, Any]:
    uid = str(vm.get("uid") or "")
    mor = str(vm.get("mor") or "")
    short = str(vm.get("shortName") or _vm_short_name_from_raw(vm) or "").strip()
    display = str(vm.get("displayName") or _vm_display_name_from_raw(vm, short) or "").strip()
    if not display:
        display = short or uid or mor or "Unnamed VM"
    if not short:
        short = display
    name = display
    nested = vm.get("vmwareState") if isinstance(vm.get("vmwareState"), dict) else {}
    power = (
        vm.get("powerState")
        or vm.get("power_state")
        or nested.get("powerState")
        or vm.get("state")
        or ""
    )
    rdp_enabled = _flag_true(vm.get("rdpEnabled"))
    guest_state = str(nested.get("guestState") or vm.get("guestState") or "")
    guest_tools = str(nested.get("guestToolsState") or vm.get("guestToolsState") or "")
    return {
        "name": name,
        "displayName": display,
        "shortName": short,
        "mor": mor,
        "uid": uid,
        "powerState": str(power),
        "guestState": guest_state,
        "guestToolsState": guest_tools,
        "rdpEnabled": rdp_enabled,
        "os": _vm_os_text(vm),
    }


def fetch_tbv3_vm_status(
    token: str,
    session_id: str,
    mor: str,
    topology_uid: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """tbv3 vm-status — same API the topology UI polls (see tbv3 HAR)."""
    sid = (session_id or "").strip()
    vm_mor = (mor or "").strip()
    version = (topology_uid or "").strip()
    if not sid or not vm_mor or not version:
        return None, None
    query = urlencode({"versionUid": version, "mor": vm_mor})
    url = f"{TBV3_API}/api/sessions/{sid}/vm-status?{query}"
    try:
        response = _request("GET", url, token, timeout=15)
    except requests.RequestException:
        return None, None
    if response.status_code == 401:
        return None, "dCloud token was rejected (401)."
    body = _json_or_text(response)
    if not isinstance(body, dict) or response.status_code >= 400:
        return None, None
    return body, None


def _fetch_session_server(
    token: str,
    site: str,
    session_id: str,
    ident: str,
) -> tuple[dict[str, Any] | None, str | None]:
    server_id = (ident or "").strip()
    if not server_id:
        return None, None
    url = f"{site_base(site)}/api/sessions/{session_id}/servers/{server_id}"
    try:
        response = _request("GET", url, token, timeout=15)
    except requests.RequestException:
        return None, None
    if response.status_code == 401:
        return None, "dCloud token was rejected (401)."
    body = _json_or_text(response)
    if not isinstance(body, dict) or response.status_code >= 400:
        return None, None
    return body, None


def fetch_vm_runtime_details(
    token: str,
    site: str,
    session_id: str,
    mor: str,
    topology_uid: str,
) -> tuple[dict[str, str], str | None]:
    """Live power/guest state from tbv3 vm-status; OS from per-server GET when present."""
    fields: dict[str, str] = {}
    status, err = fetch_tbv3_vm_status(token, session_id, mor, topology_uid)
    if err:
        return fields, err
    if status:
        state = status.get("vmwareState") if isinstance(status.get("vmwareState"), dict) else {}
        power = str(state.get("powerState") or status.get("powerState") or "")
        if power:
            fields["powerState"] = power
        guest = str(state.get("guestState") or "")
        if guest:
            fields["guestState"] = guest
        tools = str(state.get("guestToolsState") or "")
        if tools:
            fields["guestToolsState"] = tools
    server, server_err = _fetch_session_server(token, site, session_id, mor)
    if is_auth_error(server_err):
        return fields, server_err
    if server:
        nested = server.get("vmwareState") if isinstance(server.get("vmwareState"), dict) else {}
        if not fields.get("powerState"):
            power = str(
                server.get("powerState")
                or server.get("power_state")
                or nested.get("powerState")
                or ""
            )
            if power:
                fields["powerState"] = power
        os_text = _vm_os_text(server)
        if os_text:
            fields["os"] = os_text
    return fields, None


def apply_tbv3_power_states(
    token: str,
    site: str,
    session_id: str,
    vms: list[dict[str, Any]],
    session: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    topo = extract_topology_uid(session)
    if not topo:
        details, err = fetch_session(token, site, session_id, expand="server")
        if is_auth_error(err):
            return list(vms), err
        topo = extract_topology_uid(details)
        session = details or session
    def enrich(vm: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        item = dict(vm)
        mor = str(item.get("mor") or "")
        runtime, err = fetch_vm_runtime_details(token, site, session_id, mor, topo)
        for key in ("powerState", "guestState", "guestToolsState", "os"):
            value = str(runtime.get(key) or "").strip()
            if value:
                item[key] = value
        return item, err

    # Each VM requires runtime and server-detail calls. Running those serially
    # made a 12-VM card wait on roughly 24 round trips.
    updated: list[dict[str, Any]] = [dict(vm) for vm in vms]
    auth_error: str | None = None
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(vms)))) as pool:
        futures = {pool.submit(enrich, vm): index for index, vm in enumerate(vms)}
        for future in as_completed(futures):
            item, err = future.result()
            updated[futures[future]] = item
            if is_auth_error(err):
                auth_error = err
    return updated, auth_error


def fetch_session(
    token: str,
    site: str,
    session_id: str,
    *,
    expand: str = "server",
) -> tuple[dict[str, Any] | None, str | None]:
    site_code = (site or "").strip().lower()
    sid = (session_id or "").strip()
    if site_code not in KNOWN_SITES:
        return None, "Datacenter must be SJC, RTP, LON, SNG, or SYD."
    if not sid:
        return None, "Session ID is required."

    url = f"{site_base(site_code)}/api/sessions/{sid}?expand={expand}"
    try:
        response = _request("GET", url, token)
    except requests.RequestException as exc:
        return None, str(exc)

    if response.status_code == 404:
        return None, f"Session {sid} not found in {site_code.upper()}."
    if response.status_code == 401:
        return None, (
            "dCloud token was rejected (401). Use Sign in to dCloud at the top of the page "
            "to log in or import from browser, then Continue."
        )
    if response.status_code >= 400:
        return None, api_message(_json_or_text(response)) or f"HTTP {response.status_code}"

    body = _json_or_text(response)
    if not isinstance(body, dict):
        return None, "Unexpected session response."
    return body, None


def _session_info_text(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dict):
        value = value.get("name") or value.get("value") or value.get("href") or ""
    if isinstance(value, list):
        value = ", ".join(
            str(part.get("name") or part.get("value") or part) if isinstance(part, dict) else str(part)
            for part in value
        )
    return str(value if value is not None else "").strip()


def _session_record_panel(
    title: str,
    records: Any,
    empty: str,
    fields: tuple[tuple[str, str], ...],
    *,
    link_key: str = "",
) -> dict[str, Any]:
    """One repeating panel (NAT, DNS, phone numbers) in dCloud's own column order."""
    items: list[dict[str, Any]] = []
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        rows = [
            [label, _session_info_text(record.get(key))]
            for key, label in fields
            if _session_info_text(record.get(key))
        ]
        if not rows:
            continue
        item: dict[str, Any] = {"rows": rows}
        if link_key:
            href = _session_info_text(record.get(link_key))
            if href.startswith("https://") or href.startswith("http://"):
                item["url"] = href
        items.append(item)
    return {"title": title, "kind": "records", "items": items, "empty": empty}


def fetch_tbv3_session_details(
    token: str,
    session_id: str,
    version_uid: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """sessionDetails from the same tbv3 call the session view page makes."""
    sid = (session_id or "").strip()
    version = (version_uid or "").strip()
    if not sid or not version:
        return None, None
    query = urlencode({"versionUid": version})
    url = f"{TBV3_API}/api/sessions/{sid}?{query}"
    try:
        response = _request("GET", url, token, timeout=30)
    except requests.RequestException as exc:
        return None, describe_request_error(exc, "dCloud")
    if response.status_code == 401:
        return None, "dCloud token was rejected (401)."
    body = _json_or_text(response)
    if not isinstance(body, dict) or response.status_code >= 400:
        return None, api_message(body) or f"HTTP {response.status_code}"
    detail = body.get("sessionDetails")
    return (detail if isinstance(detail, dict) else None), None


def session_info_panels(
    token: str,
    site: str,
    session_id: str,
) -> tuple[dict[str, Any], str | None]:
    """Build the Session Details panels dCloud shows on its own session page."""
    details, error = fetch_session(token, site, session_id, expand="all")
    if error or not isinstance(details, dict):
        return {}, error or "Could not load session details."

    expand = details.get("expand") if isinstance(details.get("expand"), dict) else {}
    event = details.get("event") if isinstance(details.get("event"), dict) else {}
    v2_network = expand.get("network") if isinstance(expand.get("network"), dict) else {}

    # The session page reads these panels from tbv3; the v2 session payload is
    # the fallback for sessions that have no topology version.
    detail, detail_error = fetch_tbv3_session_details(
        token,
        str(details.get("uid") or session_id),
        str(details.get("topologyVersionUid") or ""),
    )
    source = detail if isinstance(detail, dict) else {}
    any_connect = source.get("anyConnect") if isinstance(source.get("anyConnect"), dict) else {}

    def pick(*values: Any) -> str:
        for value in values:
            text = _session_info_text(value)
            if text:
                return text
        return ""

    licenses = source.get("sessionLicenses")
    license_text = (
        pick(licenses)
        if isinstance(licenses, list) and licenses
        else "There are no session Licenses configured in this demo."
    )
    vpn_server = pick(any_connect.get("vpnServer"), v2_network.get("vpnServer"))
    vpn_user = pick(any_connect.get("vpnUserIds"), v2_network.get("vpnUserIds"))
    vpn_password = pick(any_connect.get("vpnPassword"), v2_network.get("vpnPassword"))
    vpn_available = pick(
        "Yes" if vpn_server else "",
        v2_network.get("vpnEnabled"),
        details.get("anyconnectAllowed"),
    )

    summary = [
        ["Parent Demo", pick(source.get("parentDemoName"), details.get("parentDemoName"))],
        ["Session Name", pick(source.get("name"), details.get("name"))],
        ["Owner", pick(source.get("owner"), details.get("owner"))],
        ["Session Id", pick(source.get("id"), details.get("uid"), session_id)],
        ["Datacenter", pick(source.get("datacenter"), str(site or "").upper())],
        ["Status", pick(format_status(details.get("status"), ""), source.get("status"))],
        ["Demo ID", pick(source.get("parentDemoId"), details.get("parentId"))],
        ["Saved Content ID", pick(source.get("activeDemoId"), details.get("activeId"))],
        ["Content Pool", pick(details.get("contentPoolName"))],
        ["Event", pick(source.get("eventName"), event.get("name"))],
        ["Start Time", pick(source.get("start"), details.get("start"))],
        ["End Time", pick(source.get("stop"), details.get("stop"))],
        ["Last Modified", pick(source.get("updated"), details.get("updated"))],
        ["VPN Available", vpn_available or "No"],
        ["Virtual Center", pick(source.get("virtualCenterId"), session_virtual_center(details))],
        ["Session Licenses", license_text],
    ]

    # Shown for display only, exactly as dCloud's session page does. Nothing
    # here is logged or written to disk.
    vpn_rows = [row for row in (["VPN", vpn_server], ["User", vpn_user]) if row[1]]

    panels: list[dict[str, Any]] = [
        {
            "title": "Session Information",
            "kind": "kv",
            "rows": [row for row in summary if row[1]],
            "empty": "No session information returned.",
        },
        {
            "title": "Cisco Secure Client Credentials",
            "kind": "kv",
            "rows": vpn_rows,
            "empty": "No VPN credentials configured",
            "password": vpn_password,
        },
        _session_record_panel(
            "Endpoint Kits",
            source.get("endpoints") if source else expand.get("endpoints"),
            "No Endpoint Kits configured",
            (("name", "Name"), ("description", "Description"), ("type", "Type")),
        ),
        _session_record_panel(
            "Public NAT IP",
            source.get("sessionPublicAddresses") if source else v2_network.get("sessionPublicAddresses"),
            "No Public NAT IP configured",
            (
                ("publicAddress", "Public IP Address"),
                ("privateAddress", "Private IP Address"),
                ("description", "Target"),
            ),
        ),
        _session_record_panel(
            "Internal NAT IP",
            source.get("sessionInternalAddresses") if source else v2_network.get("sessionInternalAddresses"),
            "No Internal NAT IP configured",
            (
                ("publicAddress", "Public IP Address"),
                ("privateAddress", "Private IP Address"),
                ("description", "Target"),
            ),
        ),
        _session_record_panel(
            "Proxy",
            source.get("sessionProxyAddresses"),
            "No Proxy configured",
            (
                ("publicAddress", "Public IP Address"),
                ("privateAddress", "Private IP Address"),
                ("description", "Target"),
            ),
        ),
        _session_record_panel(
            "Phone Numbers",
            source.get("sessionDids") if source else v2_network.get("sessionDids"),
            "No Phone Numbers configured",
            (
                ("did", "External (DID)"),
                ("dn", "Internal (DN)"),
                ("description", "Description"),
            ),
        ),
        _session_record_panel(
            "DNS",
            source.get("sessionDnsEntries") if source else v2_network.get("sessionDnsEntries"),
            "No DNS entries configured",
            (("type", "Type"), ("name", "DNS Name")),
        ),
        _session_record_panel(
            "DNS Assets",
            source.get("sessionDnsAssets") if source else v2_network.get("sessionDnsAssets"),
            "No DNS assets configured",
            (("name", "DNS Name"),),
        ),
        _session_record_panel(
            "Documents",
            source.get("documents") if source else expand.get("documents"),
            "No documents configured",
            (("name", "Name"), ("mimeType", "Type")),
            link_key="documentLink",
        ),
        _session_record_panel(
            "Shared With",
            source.get("sharedWith") if source else expand.get("sharedWith"),
            "Not shared with anyone",
            (("fullName", "Name"), ("userId", "User ID"), ("email", "Email")),
        ),
    ]
    return {
        "site": str(site or "").upper(),
        "sessionId": pick(source.get("id"), details.get("uid"), session_id),
        "name": pick(source.get("name"), details.get("name")),
        "viewUrl": session_view_url(site, str(session_id), session=details),
        "panels": panels,
        "note": detail_error or "",
    }, None


def _session_server_list(details: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(details, dict):
        return []
    expand = details.get("expand") if isinstance(details.get("expand"), dict) else {}
    candidates = [
        expand.get("servers"),
        expand.get("server"),
        details.get("servers"),
        details.get("server"),
    ]
    for raw in candidates:
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, dict):
            inner = raw.get("content") or raw.get("servers") or raw.get("server")
            if isinstance(inner, list):
                return [item for item in inner if isinstance(item, dict)]
    return []


def list_session_vms(token: str, site: str, session_id: str) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
    details, err = fetch_session(token, site, session_id, expand="server")
    if err or not details:
        return [], {}, err or "Could not load session."
    servers = _session_server_list(details)
    if not servers:
        extra, extra_err = fetch_session(token, site, session_id, expand="all")
        if extra_err and is_auth_error(extra_err):
            return [], details, extra_err
        if extra:
            details = extra
            servers = _session_server_list(extra)
    vms = [summarize_vm(vm) for vm in servers]
    topology_uid = extract_topology_container_uid(details)
    version_uid = extract_topology_uid(details)
    if not topology_uid and version_uid:
        topology_uid = fetch_tbv3_session_topology_uid(token, session_id, version_uid)
    if topology_uid:
        vms = enrich_vms_with_topology_names(token, vms, topology_uid)
    return vms, details, None


_TBV3_UID_RE = re.compile(r"^[a-z0-9]{12,}$", re.I)


def _looks_like_tbv3_uid(value: str) -> bool:
    text = (value or "").strip()
    if not text or text.isdigit():
        return False
    return bool(_TBV3_UID_RE.match(text))


def _topology_uid_from_href(href: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    parsed = urlparse(href)
    path = parsed.path or href
    for pattern in (r"/edit/([^/?#]+)", r"/api/topologies/([^/?#/]+)"):
        match = re.search(pattern, path, re.I)
        if not match:
            continue
        uid = match.group(1).strip()
        if uid and not uid.isdigit():
            return uid
    return ""


def extract_content_topology_uid(content: dict[str, Any], site: str = "") -> str:
    """Resolve tbv3 topology UID from a dCloud custom-content record."""
    if not isinstance(content, dict):
        return ""
    for key in (
        "topologyUid",
        "topologyUID",
        "topologyId",
        "topologyVersionUid",
        "versionUid",
        "versionUID",
    ):
        uid = str(content.get(key) or "").strip()
        if uid and not uid.isdigit():
            return uid
    nested = content.get("topology")
    if isinstance(nested, dict):
        uid = str(nested.get("uid") or "").strip()
        if uid and not uid.isdigit():
            return uid
    href = edit_topology_url(site, "", content)
    found = _topology_uid_from_href(href)
    if found:
        return found
    for link_key in ("links", "_links"):
        raw = content.get(link_key)
        links: list[Any] = raw if isinstance(raw, list) else []
        if isinstance(raw, dict):
            links = list(raw.values())
        for link in links:
            if not isinstance(link, dict):
                continue
            rel = str(link.get("rel") or link.get("name") or "").lower()
            href = str(link.get("href") or "").strip()
            if rel in {"edittopology", "topology", "self"} or "topology" in rel:
                found = _topology_uid_from_href(href)
                if found:
                    return found
    return ""


def fetch_tbv3_topology(
    token: str,
    topology_uid: str,
) -> tuple[dict[str, Any] | None, str | None]:
    uid = (topology_uid or "").strip()
    if not uid:
        return None, "Topology UID is required."
    url = f"{TBV3_API}/api/topologies/{uid}"
    try:
        response = _request("GET", url, token, timeout=30)
    except requests.RequestException as exc:
        return None, str(exc)
    if response.status_code == 401:
        return None, "dCloud token was rejected (401)."
    body = _json_or_text(response)
    if response.status_code >= 400 or not isinstance(body, dict):
        message = api_message(body) if isinstance(body, dict) else f"HTTP {response.status_code}"
        return None, message or f"Topology {uid} not found."
    return body, None


def fetch_tbv3_topology_vms(
    token: str,
    topology_uid: str,
) -> tuple[list[dict[str, Any]], str | None]:
    uid = (topology_uid or "").strip()
    if not uid:
        return [], "Topology UID is required."
    url = f"{TBV3_API}/api/topologies/{uid}/vms"
    try:
        response = _request("GET", url, token, timeout=30)
    except requests.RequestException as exc:
        return [], str(exc)
    if response.status_code == 401:
        return [], "dCloud token was rejected (401)."
    body = _json_or_text(response)
    if response.status_code >= 400:
        message = api_message(body) if isinstance(body, dict) else f"HTTP {response.status_code}"
        return [], message or f"Could not load VMs for topology {uid}."
    if not isinstance(body, dict):
        return [], "Unexpected topology VMs response."
    embedded = body.get("_embedded") if isinstance(body.get("_embedded"), dict) else {}
    rows = embedded.get("vms") or body.get("vms") or []
    if not isinstance(rows, list):
        return [], "Unexpected topology VMs response."
    return [item for item in rows if isinstance(item, dict)], None


def extract_topology_container_uid(session: dict[str, Any] | None) -> str:
    if not session:
        return ""
    nested = session.get("topology")
    if isinstance(nested, dict):
        uid = str(nested.get("uid") or "").strip()
        if uid:
            return uid
    for key in ("topologyUid", "topologyUID"):
        uid = str(session.get(key) or "").strip()
        if uid:
            return uid
    return ""


def fetch_tbv3_session_topology_uid(
    token: str,
    session_id: str,
    version_uid: str,
) -> str:
    sid = (session_id or "").strip()
    version = (version_uid or "").strip()
    if not sid or not version:
        return ""
    query = urlencode({"versionUid": version})
    url = f"{TBV3_API}/api/sessions/{sid}?{query}"
    try:
        response = _request("GET", url, token, timeout=20)
    except requests.RequestException:
        return ""
    if response.status_code >= 400:
        return ""
    body = _json_or_text(response)
    if not isinstance(body, dict):
        return ""
    return extract_topology_container_uid(body)


def _topology_vm_display_name(vm: dict[str, Any]) -> str:
    return str(vm.get("name") or "").strip()


def _topology_vmware_name(vm: dict[str, Any]) -> str:
    """VMware/hypervisor name from topology builder Advanced Settings (same /vms payload)."""
    advanced = vm.get("advancedSettings")
    if isinstance(advanced, dict):
        hypervisor = str(advanced.get("nameInHypervisor") or "").strip()
        if hypervisor:
            return hypervisor
    return _topology_vm_display_name(vm)


def enrich_vms_with_topology_names(
    token: str,
    vms: list[dict[str, Any]],
    topology_uid: str,
) -> list[dict[str, Any]]:
    uid = (topology_uid or "").strip()
    if not uid or not vms:
        return vms
    raw_vms, err = fetch_tbv3_topology_vms(token, uid)
    if err or not raw_vms:
        return vms
    by_vmware: dict[str, str] = {}
    by_inventory: dict[str, str] = {}
    vmware_by_inventory: dict[str, str] = {}
    for item in raw_vms:
        display = _topology_vm_display_name(item)
        vmware = _topology_vmware_name(item)
        inventory = str(item.get("inventoryVmId") or "").strip()
        if vmware and display:
            by_vmware[vmware.lower()] = display
        if inventory and display:
            by_inventory[inventory] = display
        if inventory and vmware:
            vmware_by_inventory[inventory] = vmware
    enriched: list[dict[str, Any]] = []
    for vm in vms:
        row = dict(vm)
        vmware = str(row.get("shortName") or row.get("name") or "").strip()
        mor = str(row.get("mor") or "").strip()
        display = str(row.get("displayName") or "").strip()
        # Topology Builder's Name is the label drawn on the topology, so it wins.
        topology_name = by_vmware.get(vmware.lower()) or by_inventory.get(mor) or ""
        if topology_name:
            display = topology_name
        elif not display:
            display = vmware
        if not vmware or vmware.lower() == display.lower():
            vmware = vmware_by_inventory.get(mor) or vmware or display
        row["displayName"] = display
        row["shortName"] = vmware or display
        row["name"] = display
        enriched.append(row)
    return enriched


def summarize_topology_vm(vm: dict[str, Any]) -> dict[str, Any]:
    """Map tbv3 saved-content VM records to the same shape as live session VMs."""
    display = _topology_vm_display_name(vm)
    vmware = _topology_vmware_name(vm)
    mor = str(vm.get("inventoryVmId") or "").strip()
    uid = str(vm.get("uid") or "").strip()
    os_family = str(vm.get("osFamily") or "").strip()
    return summarize_vm(
        {
            "displayName": display,
            "shortName": vmware,
            "name": display,
            "mor": mor,
            "uid": uid,
            "osFamily": os_family,
            "powerState": "",
            "guestToolsState": "",
            "guestState": "",
        }
    )


def resolve_content_topology_uid(
    token: str,
    site: str,
    content_id: str,
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """Return (topology_uid, content_record, topology_record, error)."""
    site_code = (site or "").strip().lower()
    raw = str(content_id or "").strip()
    if site_code not in KNOWN_SITES:
        return "", None, None, "Datacenter must be SJC, RTP, LON, SNG, or SYD."
    if not raw:
        return "", None, None, "Saved content ID is required."

    content: dict[str, Any] | None = None
    topology_uid = raw if _looks_like_tbv3_uid(raw) else ""
    if not topology_uid:
        content = fetch_content(token, site_code, raw)
        if not content:
            return "", None, None, f"Saved content {raw} not found in {site_code.upper()}."
        topology_uid = extract_content_topology_uid(content, site_code)
    if not topology_uid:
        return (
            "",
            content,
            None,
            "Could not resolve a v3 topology UID for this saved content. "
            "Open Edit Topology in dCloud and paste the ID from the URL "
            f"({TBV3_UI}/edit/…), or use an active session instead.",
        )

    topology, err = fetch_tbv3_topology(token, topology_uid)
    if err:
        return "", content, None, err
    return topology_uid, content, topology, None


def list_content_vms(
    token: str,
    site: str,
    content_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
    topology_uid, content, topology, err = resolve_content_topology_uid(token, site, content_id)
    if err or not topology_uid or not topology:
        return [], {}, err or "Could not load saved content."

    vms_raw, vm_err = fetch_tbv3_topology_vms(token, topology_uid)
    if vm_err:
        return [], {}, vm_err

    site_code = (site or "").strip().lower()
    numeric_id = extract_demo_numeric_id(content) if content else ""
    if not numeric_id and not _looks_like_tbv3_uid(content_id):
        numeric_id = str(content_id or "").strip()
    name = str((content or {}).get("name") or topology.get("name") or "").strip()
    demo_id = str(topology.get("demoId") or (content or {}).get("parentId") or "").strip()
    topo_dc = str(topology.get("datacenter") or "").strip().lower()
    details: dict[str, Any] = {
        "name": name,
        "status": str(topology.get("status") or "SAVED_CONTENT"),
        "demoId": demo_id,
        "contentId": numeric_id,
        "topologyUid": topology_uid,
        "datacenter": topo_dc,
        "source": "content",
        "viewUrl": tbv3_edit_url(topology_uid),
    }
    if topo_dc and topo_dc != site_code:
        details["siteMismatch"] = f"Topology is in {topo_dc.upper()}, not {site_code.upper()}."
    vms = [summarize_topology_vm(vm) for vm in vms_raw]
    return vms, details, None


def _norm_name(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _numeric_id(*values: Any) -> str:
    for value in values:
        if value is None or isinstance(value, (dict, list, bool)):
            continue
        text = str(value).strip()
        if text.isdigit():
            return text
    return ""


def extract_demo_numeric_id(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    found = _numeric_id(
        obj.get("uid"),
        obj.get("demoId"),
        obj.get("demoUid"),
        obj.get("contentId"),
        obj.get("contentUid"),
        obj.get("id"),
        obj.get("fkrootDemoId"),
        obj.get("parentId"),
    )
    if found:
        return found
    for nest in ("demo", "content", "item", "data", "result"):
        nested = extract_demo_numeric_id(obj.get(nest))
        if nested:
            return nested
    return ""


def extract_parent_content_id(obj: Any, *, saved_id: str = "") -> str:
    """Immediate parent demo ID for a saved copy. Empty if unknown or same as the saved ID.

    This is the demo the save came from (save-of-a-save → the previous save).
    Do not treat CAMGR's fkrootDemoId as the parent — that is the original base.
    """
    saved = str(saved_id or "").strip()
    if not isinstance(obj, dict):
        return ""
    for key in ("parentId", "parentDemoId", "publishedId"):
        value = str(obj.get(key) or "").strip()
        if value.isdigit() and value != saved:
            return value
    for link in obj.get("links") or []:
        if not isinstance(link, dict):
            continue
        rel = str(link.get("rel") or "").lower()
        href = str(link.get("href") or link.get("uri") or "")
        if "parent" not in rel:
            continue
        hit = re.search(r"/contents/(\d+)", href)
        if hit and hit.group(1) != saved:
            return hit.group(1)
    for nest in ("demo", "content", "item", "data", "result"):
        found = extract_parent_content_id(obj.get(nest), saved_id=saved)
        if found:
            return found
    return ""


_ROOT_ID_KEYS = {"fkrootdemoid", "rootdemoid", "rootid"}


def extract_root_content_id(obj: Any, *, saved_id: str = "") -> str:
    """Original base demo ID. Empty if unknown or same as the saved ID.

    CAMGR spells it fkrootDemoId; dCloud content details use _rootDemoId. Match
    on the name with underscores and case ignored so a new spelling of the same
    field does not silently leave the Root ID column blank.
    """
    saved = str(saved_id or "").strip()
    if not isinstance(obj, dict):
        return ""
    for key, raw in obj.items():
        if str(key).replace("_", "").lower() not in _ROOT_ID_KEYS:
            continue
        value = str(raw or "").strip()
        if value.isdigit() and value != saved:
            return value
    for nest in ("demo", "content", "item", "data", "result"):
        found = extract_root_content_id(obj.get(nest), saved_id=saved)
        if found:
            return found
    return ""


def root_content_id_is_self(obj: Any, *, saved_id: str = "") -> bool:
    """True when the payload names this saved content as its own root.

    That is an answer, not a missing value: the demo is the original base.
    """
    saved = str(saved_id or "").strip()
    if not saved or not isinstance(obj, dict):
        return False
    for key, raw in obj.items():
        if str(key).replace("_", "").lower() in _ROOT_ID_KEYS and str(raw or "").strip() == saved:
            return True
    return any(
        root_content_id_is_self(obj.get(nest), saved_id=saved)
        for nest in ("demo", "content", "item", "data", "result")
    )


def _owner_name(item: dict[str, Any]) -> str:
    owner = item.get("owner") or item.get("createdBy") or item.get("username") or ""
    if isinstance(owner, dict):
        return str(owner.get("username") or owner.get("name") or owner.get("uid") or "").strip()
    return str(owner or "").strip()


def _state_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("state", "states", "status", "lifecycleState"):
        value = item.get(key)
        if isinstance(value, list):
            parts.extend(str(part) for part in value if part)
        elif value:
            parts.append(str(value))
    for flag in ("published", "promoted"):
        if item.get(flag) is True:
            parts.append(flag)
    return " ".join(parts).lower()


def _as_item_list(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if not isinstance(body, dict):
        return []
    for key in ("content", "contents", "demos", "items"):
        value = body.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    embedded = body.get("_embedded")
    if isinstance(embedded, dict):
        for value in embedded.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return [item for item in value if isinstance(item, dict)]
    return []


def catalog_search(
    token: str,
    site: str,
    name: str,
    strategy: str = "EXACT",
) -> tuple[list[dict[str, Any]], str | None]:
    """POST /api/global-catalog/search — same call as the catalog Exact Match checkbox."""
    url = f"{site_base(site)}/api/global-catalog/search"
    payload = {"criteria": name, "strategy": strategy}
    try:
        response = _request("POST", url, token, json_body=payload, timeout=30)
    except requests.RequestException as exc:
        return [], str(exc)
    if response.status_code == 401:
        return [], "dCloud token was rejected (401)."
    body = _json_or_text(response)
    if response.status_code >= 400:
        return [], api_message(body) or f"HTTP {response.status_code}"
    items = []
    if isinstance(body, dict):
        items = ((body.get("_embedded") or {}).get("contentItems")) or []
    if not isinstance(items, list):
        return [], None
    return [item for item in items if isinstance(item, dict)], None


def _admin_search_value(item: dict[str, Any], *keys: str) -> str:
    values: list[str] = []
    for key in keys:
        value = item.get(key)
        if isinstance(value, dict):
            value = value.get("href") or value.get("id") or ""
        if isinstance(value, list):
            values.extend(str(part) for part in value if part is not None)
        elif value is not None:
            values.append(str(value))
    return " ".join(values)


def fetch_admin_records(
    token: str,
    site: str,
    *,
    resource: str,
    refresh: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    """Load one complete DC-local admin list, with a short server cache."""
    if resource not in {"demos", "events", "sessions"}:
        return [], "Unsupported dCloud admin search resource."
    cache_key = (str(site or "").lower(), resource)
    now = time.time()
    with _admin_search_cache_lock:
        cached = _admin_search_cache.get(cache_key)
        if cached and not refresh and now - cached[0] < _ADMIN_SEARCH_CACHE_SECONDS:
            return list(cached[1]), None
    try:
        response = _request(
            "GET",
            f"{site_base(site)}/api/admin/{resource}",
            token,
            timeout=60,
        )
    except requests.RequestException as exc:
        return [], describe_request_error(exc, "dCloud")
    if response.status_code == 401:
        return [], "dCloud token was rejected (401)."
    body = _json_or_text(response)
    if response.status_code >= 400:
        return [], api_message(body) or f"HTTP {response.status_code}"
    # Events is returned as a bare list; Content and Sessions wrap rows in
    # {"content": [...]}. Normalize both shapes here.
    records = body if isinstance(body, list) else body.get("content") if isinstance(body, dict) else []
    if not isinstance(records, list):
        return [], None
    records = [item for item in records if isinstance(item, dict)]
    with _admin_search_cache_lock:
        _admin_search_cache[cache_key] = (now, records)
    return list(records), None


def list_event_sessions(
    token: str,
    site: str,
    event_id: str,
    *,
    refresh: bool = False,
) -> tuple[dict[str, Any], str | None]:
    """Return one admin event and its complete session rows from a datacenter."""
    site_code = str(site or "").strip().lower()
    wanted = str(event_id or "").strip()
    if site_code not in KNOWN_SITES:
        return {}, "Datacenter must be SJC, RTP, LON, SNG, or SYD."
    if not wanted.isdigit():
        return {}, "Enter a numeric event ID."

    events, event_error = fetch_admin_records(
        token, site_code, resource="events", refresh=refresh
    )
    if event_error:
        return {}, event_error
    event = next(
        (row for row in events if str(row.get("uid") or "").strip() == wanted),
        None,
    )
    if not event:
        return {}, f"Event {wanted} was not found in {site_code.upper()}."

    sessions, session_error = fetch_admin_records(
        token, site_code, resource="sessions", refresh=refresh
    )
    if session_error:
        return {}, session_error

    rows: list[dict[str, Any]] = []
    for session in sessions:
        linked_event = session.get("event")
        linked_id = (
            str(linked_event.get("uid") or "").strip()
            if isinstance(linked_event, dict)
            else str(linked_event or "").strip()
        )
        if linked_id != wanted:
            continue
        sid = str(session.get("uid") or "").strip()
        if not sid:
            continue
        status = session.get("status")
        rows.append(
            {
                "sessionId": sid,
                "eventId": wanted,
                "name": str(session.get("name") or session.get("parentDemoName") or "").strip(),
                "owner": str(session.get("owner") or "").strip(),
                "student": str((linked_event or {}).get("student") or "").strip()
                if isinstance(linked_event, dict)
                else "",
                "demoId": str(session.get("parentId") or "").strip(),
                "activeId": str(session.get("activeId") or "").strip(),
                "virtualCenter": str(session.get("virtualCenter") or "").strip(),
                "start": str(session.get("start") or "").strip(),
                "stop": str(session.get("stop") or "").strip(),
                "status": format_status(status),
                "rawStatus": status,
                "active": is_active_status(status),
                "canReset": session.get("canReset") is True,
                "viewUrl": session_view_url(site_code, sid, session=session),
            }
        )
    rows.sort(key=lambda row: int(row["sessionId"]) if row["sessionId"].isdigit() else 0)
    return {
        "site": site_code,
        "eventId": wanted,
        "name": str(event.get("name") or "").strip(),
        "status": format_status(event.get("status")),
        "approval": format_status(event.get("approval")),
        "eventStart": str(event.get("eventStart") or "").strip(),
        "eventEnd": str(event.get("eventEnd") or "").strip(),
        "sessionCount": int(event.get("sessionCount") or len(rows)),
        "sessions": rows,
    }, None


def admin_records_cached_at(site: str, *, resource: str) -> float | None:
    """Return when a DC-local admin list was downloaded from dCloud."""
    cache_key = (str(site or "").lower(), resource)
    with _admin_search_cache_lock:
        cached = _admin_search_cache.get(cache_key)
        return cached[0] if cached else None


def fetch_admin_content_panels(
    token: str,
    site: str,
    content_id: str,
    *,
    selected_filter_ids: list[Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Read the non-editing Content panels exposed by dCloud admin APIs."""
    site_code = str(site or "").strip().lower()
    cid = str(content_id or "").strip()
    if site_code not in KNOWN_SITES or not cid:
        return {}, {"content": "Invalid datacenter or content ID."}

    def get_json(path: str) -> tuple[dict[str, Any], str | None]:
        try:
            response = _request(
                "GET",
                f"{site_base(site_code)}{path}",
                token,
                timeout=60,
            )
        except requests.RequestException as exc:
            return {}, describe_request_error(exc, "dCloud")
        body = _json_or_text(response)
        if response.status_code == 401:
            return {}, "dCloud token was rejected (401)."
        if response.status_code >= 400:
            return {}, api_message(body) or f"HTTP {response.status_code}"
        return (body if isinstance(body, dict) else {}), None

    paths = {
        "permissions": f"/api/admin/demos/{quote(cid)}/usergroups",
        "ssoGroups": f"/api/admin/demos/{quote(cid)}/ssogroups",
        "filters": "/api/admin/demos/filters",
        "resources": f"/api/admin/demos/{quote(cid)}/resource-usage",
    }
    payloads: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for key, path in paths.items():
        payload, error = get_json(path)
        payloads[key] = payload
        if error:
            errors[key] = error

    access_levels: list[dict[str, str]] = []
    user_groups: list[dict[str, str]] = []
    permission_rows = payloads["permissions"].get("content") or []
    for row in permission_rows if isinstance(permission_rows, list) else []:
        if not isinstance(row, dict):
            continue
        normalized = {
            "id": str(row.get("uid") or ""),
            "name": str(row.get("name") or row.get("uid") or ""),
        }
        if str(row.get("type") or "").lower() == "special":
            access_levels.append(normalized)
        else:
            user_groups.append(normalized)

    sso_groups: list[dict[str, str]] = []
    raw_sso = payloads["ssoGroups"].get("ssoGroups") or []
    for row in raw_sso if isinstance(raw_sso, list) else []:
        if isinstance(row, dict):
            sso_groups.append(
                {
                    "id": str(row.get("uid") or row.get("id") or ""),
                    "name": str(row.get("name") or row.get("uid") or row.get("id") or ""),
                }
            )
        elif row:
            sso_groups.append({"id": str(row), "name": str(row)})

    selected_ids = {str(value) for value in (selected_filter_ids or [])}
    selected_filters: list[dict[str, str]] = []

    def walk_filters(rows: Any, group: str) -> None:
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            uid = str(row.get("uid") or "")
            if uid and uid in selected_ids:
                selected_filters.append(
                    {
                        "id": uid,
                        "name": str(row.get("name") or uid).strip(),
                        "group": group,
                    }
                )
            walk_filters(row.get("children"), group)

    filter_groups = payloads["filters"].get("filterGroups") or []
    for group in filter_groups if isinstance(filter_groups, list) else []:
        if not isinstance(group, dict):
            continue
        walk_filters(group.get("filters"), str(group.get("name") or "Other").strip())

    resources = payloads["resources"].get("resources") or {}
    return {
        "permissions": {
            "accessLevels": access_levels,
            "userGroups": user_groups,
            "ssoGroups": sso_groups,
        },
        "filters": selected_filters,
        "resources": resources if isinstance(resources, dict) else {},
    }, errors


def search_admin_records(
    token: str,
    site: str,
    query: str,
    *,
    resource: str,
    limit: int = 50,
    refresh: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    """Search the DC-local admin Content or Sessions table, which the UI filters client-side."""
    records, error = fetch_admin_records(
        token,
        site,
        resource=resource,
        refresh=refresh,
    )
    if error:
        return [], error
    needle = str(query or "").strip().casefold()
    if not needle:
        return [], None
    keys = (
        ("name", "uid", "guid", "demoId", "owner", "description", "state", "type")
        if resource == "demos"
        else (
            "name",
            "uid",
            "guid",
            "parentId",
            "activeId",
            "owner",
            "parentDemoName",
            "status",
            "contentPoolName",
        )
    )
    matches: list[dict[str, Any]] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        haystack = _admin_search_value(item, *keys).casefold()
        if needle in haystack:
            matches.append(item)

    def exact_rank(item: dict[str, Any]) -> int:
        name = str(item.get("name") or "").strip().casefold()
        ids = {
            str(item.get(key) or "").strip().casefold()
            for key in ("uid", "demoId", "parentId", "activeId", "guid")
        }
        return 0 if needle == name or needle in ids else 1

    matches.sort(
        key=lambda item: str(item.get("updated") or item.get("published") or ""),
        reverse=True,
    )
    matches.sort(key=exact_rank)
    return matches[: max(1, int(limit))], None


def fetch_catalog_item(token: str, site: str, item: dict[str, Any]) -> dict[str, Any] | None:
    slug = str(item.get("id") or "").strip()
    href = ""
    links = item.get("_links") if isinstance(item.get("_links"), dict) else {}
    self_link = links.get("self") if isinstance(links.get("self"), dict) else {}
    href = str((self_link or {}).get("href") or "").strip()
    urls: list[str] = []
    if href:
        urls.append(href)
    if slug:
        urls.append(f"{site_base(site)}/api/global-catalog/{quote(slug)}")
        urls.append(f"{site_base(site)}/dCloudAPI/global-catalog/{quote(slug)}")
    seen: set[str] = set()
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            response = _request("GET", url, token, timeout=20)
        except requests.RequestException:
            continue
        body = _json_or_text(response)
        if response.status_code < 400 and isinstance(body, dict):
            return body
    return None


def _pick_published_admin(matches: list[dict[str, Any]]) -> dict[str, Any] | None:
    published_admin: list[dict[str, Any]] = []
    for item in matches:
        owner = _owner_name(item).lower()
        state = _state_text(item)
        if owner == "admin" and "published" in state:
            published_admin.append(item)
    pool = published_admin
    if not pool:
        return None
    promoted = [item for item in pool if "promoted" in _state_text(item)]
    chosen = promoted or pool

    def sort_key(item: dict[str, Any]) -> str:
        return str(
            item.get("updated")
            or item.get("lastUpdated")
            or item.get("modifiedOn")
            or item.get("publishedOn")
            or ""
        )

    chosen.sort(key=sort_key, reverse=True)
    return chosen[0]


def find_admin_demo_by_name(
    token: str,
    site: str,
    name: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
    """Admin content list (same table as /api/admin/demos). Prefer admin + published/promoted."""
    wanted = _norm_name(name)
    encoded = quote(name)
    urls = [
        f"{site_base(site)}/api/admin/demos?name={encoded}",
        f"{site_base(site)}/api/admin/demos?search={encoded}",
        f"{site_base(site)}/api/contents?name={encoded}",
        f"{site_base(site)}/api/admin/demos",
    ]
    last_err: str | None = None
    scanned_full = False
    for url in urls:
        is_full = url.endswith("/api/admin/demos")
        if scanned_full:
            break
        try:
            response = _request("GET", url, token, timeout=90)
        except requests.RequestException as exc:
            last_err = str(exc)
            continue
        if response.status_code == 401:
            return None, [], "dCloud token was rejected (401)."
        if response.status_code >= 400:
            last_err = api_message(_json_or_text(response)) or f"HTTP {response.status_code}"
            continue
        items = _as_item_list(_json_or_text(response))
        if is_full:
            scanned_full = True
        matches = [
            item
            for item in items
            if _norm_name(item.get("name") or item.get("contentName") or "") == wanted
        ]
        if not matches and not is_full:
            continue
        picked = _pick_published_admin(matches)
        return picked, matches, None
    return None, [], last_err


def lookup_demo_id_for_site(
    token: str,
    site: str,
    name: str,
    *,
    source_site: str = "",
    source_demo_id: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "site": site,
        "id": "",
        "name": name,
        "source": "",
        "owner": "",
        "state": "",
        "catalogId": "",
        "note": "",
    }
    source_code = (source_site or "").strip().lower()
    demo_id = str(source_demo_id or "").strip()
    if source_code and site == source_code and demo_id:
        result["id"] = demo_id
        result["source"] = "session"
        result["note"] = "from this session"
        return result
    if not _norm_name(name):
        result["source"] = "not_found"
        result["note"] = "session has no name"
        return result

    exact: list[dict[str, Any]] = []
    last_err: str | None = None
    items: list[dict[str, Any]] = []
    for strategy in ("SUBSTRING", "EXACT"):
        found, err = catalog_search(token, site, name, strategy)
        last_err = err or last_err
        if found:
            items = found
            break
    exact = [item for item in items if _norm_name(item.get("name")) == _norm_name(name)]

    if not exact:
        result["source"] = "not_found"
        result["note"] = last_err or "no exact catalog match"
        return result

    exact.sort(key=lambda item: 0 if str(item.get("contentType") or "").upper() == "DEMO" else 1)
    item = exact[0]
    result["catalogId"] = str(item.get("id") or "")
    numeric = extract_demo_numeric_id(item)
    source = "catalog"
    if not numeric:
        detail = fetch_catalog_item(token, site, item)
        numeric = extract_demo_numeric_id(detail or {})
    if not numeric:
        admin_hit, _matches, admin_err = find_admin_demo_by_name(token, site, name)
        if admin_hit:
            numeric = extract_demo_numeric_id(admin_hit)
            result["owner"] = _owner_name(admin_hit)
            result["state"] = _state_text(admin_hit)
            source = "catalog+admin"
        elif admin_err:
            last_err = admin_err

    if numeric:
        result["id"] = numeric
        result["source"] = source
        bits = ["catalog exact match"]
        if result["owner"]:
            bits.append(result["owner"])
        if "published" in (result["state"] or "") or source.startswith("catalog"):
            bits.append("published")
        if "promoted" in (result["state"] or ""):
            bits.append("promoted")
        if len(exact) > 1:
            bits.append(f"{len(exact)} name hits")
        result["note"] = " · ".join(bits)
        return result

    result["source"] = "not_found"
    result["note"] = last_err or "in catalog, but no numeric content ID"
    return result


def lookup_demo_ids_across_sites(
    token: str,
    name: str,
    *,
    source_site: str = "",
    source_demo_id: str = "",
) -> dict[str, dict[str, Any]]:
    """Resolve every DC ID from one global-catalog item, with legacy fallback."""
    source_code = (source_site or "").strip().lower()
    catalog_site = source_code if source_code in KNOWN_SITES else "rtp"

    items: list[dict[str, Any]] = []
    last_err: str | None = None
    for strategy in ("SUBSTRING", "EXACT"):
        found, err = catalog_search(token, catalog_site, name, strategy)
        last_err = err or last_err
        if found:
            items = found
            break
    exact = [item for item in items if _norm_name(item.get("name")) == _norm_name(name)]
    exact.sort(key=lambda item: 0 if str(item.get("contentType") or "").upper() == "DEMO" else 1)

    if exact:
        item = exact[0]
        detail = fetch_catalog_item(token, catalog_site, item)
        availability = (detail or {}).get("availability")
        if isinstance(availability, list) and availability:
            results: dict[str, dict[str, Any]] = {
                site: {
                    "site": site,
                    "id": "",
                    "name": name,
                    "source": "not_found",
                    "owner": "",
                    "state": "",
                    "catalogId": str(item.get("id") or ""),
                    "note": "not available in global catalog",
                }
                for site in SITES
            }
            for available in availability:
                if not isinstance(available, dict):
                    continue
                site = str(available.get("datacenter") or "").strip().lower()
                numeric = extract_demo_numeric_id(available)
                if site not in KNOWN_SITES or not numeric:
                    continue
                results[site].update(
                    {
                        "id": numeric,
                        "source": "global_catalog",
                        "note": "global catalog availability",
                    }
                )
            # Preserve the authoritative ID from the session/content that was loaded.
            source_id = str(source_demo_id or "").strip()
            if source_code in KNOWN_SITES and source_id:
                results[source_code].update(
                    {
                        "id": source_id,
                        "source": "session",
                        "note": "from this session",
                    }
                )
            return results

    # Older catalog responses may not expose availability. Keep the established
    # per-DC lookup so those demos still work.
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=len(SITES)) as pool:
        futures = {
            pool.submit(
                lookup_demo_id_for_site,
                token,
                site,
                name,
                source_site=source_site,
                source_demo_id=source_demo_id,
            ): site
            for site in SITES
        }
        for future in as_completed(futures):
            site = futures[future]
            try:
                results[site] = future.result()
            except Exception as exc:
                results[site] = {
                    "site": site,
                    "id": "",
                    "name": name,
                    "source": "error",
                    "note": str(exc),
                }
    if last_err and all(not row.get("id") for row in results.values()):
        for row in results.values():
            if not row.get("note"):
                row["note"] = last_err
    return results


BLOCKING_TIMESLOT_TYPES = frozenset({"CONFLICT", "UNAVAILABLE", "UNAVAILABILITY"})
MAX_NEXT_SLOT_DELAY = timedelta(days=1)
SHORTER_SESSION_CHOICES_DAYS = (30, 14, 7, 3, 1)


def _timeslot_interval(slot: dict[str, Any]) -> tuple[datetime, datetime] | None:
    start = parse_schedule_datetime(str(slot.get("start") or ""))
    end = parse_schedule_datetime(str(slot.get("stop") or slot.get("end") or ""))
    if not start or not end or end <= start:
        return None
    return start, end


def _is_blocking_timeslot(slot: dict[str, Any]) -> bool:
    if slot.get("available") is False:
        return True
    slot_type = str(slot.get("type") or "").upper()
    return slot_type in BLOCKING_TIMESLOT_TYPES


def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda item: item[0])
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _blocking_intervals(
    timeslots: list[dict[str, Any]],
    range_start: datetime,
    range_end: datetime,
) -> list[tuple[datetime, datetime]]:
    blocks: list[tuple[datetime, datetime]] = []
    for slot in timeslots or []:
        if not _is_blocking_timeslot(slot):
            continue
        interval = _timeslot_interval(slot)
        if not interval:
            continue
        start, end = interval
        if end <= range_start or start >= range_end:
            continue
        blocks.append((max(start, range_start), min(end, range_end)))
    return _merge_intervals(blocks)


def window_has_conflict(
    begin: datetime,
    end: datetime,
    timeslots: list[dict[str, Any]],
) -> bool:
    if end <= begin:
        return True
    for block_start, block_end in _blocking_intervals(timeslots, begin, end):
        if block_end > begin and block_start < end:
            return True
    return False


def find_next_available_window(
    desired_start: datetime,
    duration: timedelta,
    timeslots: list[dict[str, Any]],
    *,
    search_end: datetime | None = None,
) -> tuple[datetime, datetime] | None:
    if duration <= timedelta(0):
        return None
    search_end = search_end or (desired_start + timedelta(days=14))
    if search_end <= desired_start:
        return None
    blocks = _blocking_intervals(timeslots, desired_start, search_end)
    cursor = desired_start
    for block_start, block_end in blocks:
        if cursor < block_start and cursor + duration <= block_start:
            return cursor, cursor + duration
        if cursor < block_end:
            cursor = block_end
    if cursor + duration <= search_end:
        return cursor, cursor + duration
    return None


def unavailable_resource_name(message: str) -> str:
    """Pull the useful resource name out of dCloud's capacity error."""
    match = re.search(
        r"resources?\s+are\s+unavailable:.*?Resource\s+'([^']+)'",
        str(message or ""),
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def find_shorter_schedule_option(
    token: str,
    site: str,
    demo_id: str,
    *,
    desired_start: datetime,
    requested_duration: timedelta,
    pool_id: str | None,
) -> tuple[datetime, datetime, int] | None:
    """Find a shorter session that can begin within the next 24 hours.

    This is advisory only. It reads the same calendar as dCloud's scheduling
    page and never submits a session.
    """
    choices = [
        days
        for days in SHORTER_SESSION_CHOICES_DAYS
        if timedelta(days=days) < requested_duration
    ]
    if not choices:
        return None
    latest_start = desired_start + MAX_NEXT_SLOT_DELAY
    longest = timedelta(days=max(choices))
    timeslots, error = fetch_content_calendar(
        token,
        site,
        demo_id,
        range_start=desired_start,
        range_end=latest_start + longest,
        pool_id=pool_id,
    )
    if error:
        return None
    for days in choices:
        duration = timedelta(days=days)
        option = find_next_available_window(
            desired_start,
            duration,
            timeslots,
            search_end=latest_start + duration,
        )
        if option and option[0] <= latest_start:
            return option[0], option[1], days
    return None


def fetch_content_calendar(
    token: str,
    site: str,
    demo_id: str,
    *,
    range_start: datetime,
    range_end: datetime,
    pool_id: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """GET /api/contents/{id}/calendar — same feed as the dCloud schedule calendar UI."""
    site_code = (site or "").strip().lower()
    demo = str(demo_id or "").strip()
    if site_code not in KNOWN_SITES:
        return [], f"Unknown datacenter {site}."
    if not demo:
        return [], "Demo / content ID is required."
    if range_end <= range_start:
        return [], "Invalid calendar range."
    params: dict[str, str] = {
        "start": _dcloud_timestamp(range_start),
        "end": _dcloud_timestamp(range_end),
    }
    if pool_id:
        params["contentPoolOptionId"] = pool_id
    url = f"{site_base(site_code)}/api/contents/{demo}/calendar?{urlencode(params)}"
    try:
        response = _request("GET", url, token, timeout=90)
    except requests.RequestException as exc:
        return [], str(exc)
    if response.status_code == 401:
        return [], "dCloud token was rejected (401)."
    body = _json_or_text(response)
    if response.status_code >= 400:
        return [], api_message(body) or f"HTTP {response.status_code}"
    if not isinstance(body, dict):
        return [], "Unexpected calendar response."
    slots = body.get("timeslots") or []
    if not isinstance(slots, list):
        return [], "Unexpected calendar response."
    return [slot for slot in slots if isinstance(slot, dict)], None


def _schedule_payload(
    demo_id: str,
    start: str,
    stop: str,
    *,
    pool_id: str | None,
    content_export: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "count": 1,
        "demoId": demo_id,
        "start": start,
        "stop": stop,
        "metrics": [
            {"name": "demoUse", "value": "developtest"},
            {"name": "revenue", "value": "Not Applicable"},
            {"name": "customerName", "value": "dcloud_bot"},
        ],
        "contentExport": CONTENT_EXPORT if content_export else CONTENT_REGULAR,
        "scenario": "null",
        "endpoints": [],
    }
    if pool_id:
        payload["contentPoolOptionId"] = pool_id
    return payload


def _pool_attempts(site: str) -> list[tuple[str, str | None]]:
    """Dev Pool first (same IDs as /sch), then Public/Core if Dev cannot take the demo."""
    if site == "syd":
        return [("SYD", None)]
    attempts: list[tuple[str, str | None]] = []
    if site in DEV_POOLS:
        attempts.append(("Dev Pool", DEV_POOLS[site]))
    attempts.append(("Public / Core Pool", CORE_POOL))
    return attempts


def fetch_content_pool_options(
    token: str,
    site: str,
    demo_id: str,
) -> tuple[list[tuple[str, str]], str]:
    """GET /dCloudAPI/demos/{id}/content-pool-scheduling-options — pools this content allows."""
    site_code = (site or "").strip().lower()
    demo = str(demo_id or "").strip()
    if site_code not in KNOWN_SITES or not demo:
        return [], "Unknown datacenter or missing demo ID."
    url = f"{site_base(site_code)}/dCloudAPI/demos/{demo}/content-pool-scheduling-options"
    try:
        response = _request("GET", url, token, timeout=30)
    except requests.RequestException as exc:
        return [], str(exc)
    body = _json_or_text(response)
    if response.status_code >= 400:
        return [], api_message(body) or f"HTTP {response.status_code}"
    embedded = body.get("_embedded") if isinstance(body, dict) else None
    rows = embedded.get("contentPoolSchedulingOptions") if isinstance(embedded, dict) else None
    if not isinstance(rows, list):
        return [], "Unexpected content pool response."
    options: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pool_id = str(row.get("id") or "").strip()
        if pool_id:
            options.append((str(row.get("title") or pool_id).strip(), pool_id))
    if not options:
        return [], "No content pools are available for this content."
    return options, ""


def _pool_rank(title: str, pool_id: str, site: str) -> int:
    """Dev pool always first, Core last, anything else in between."""
    if pool_id == DEV_POOLS.get(site) or "content_dev" in title.lower().replace(" ", "_"):
        return 0
    if pool_id == CORE_POOL:
        return 2
    return 1


def _pool_attempts_for_demo(
    token: str,
    site: str,
    demo_id: str,
    *,
    progress: Progress | None = None,
) -> list[tuple[str, str | None]]:
    """Ask dCloud which pools this content can use; guessing wrong yields a confusing error."""
    options, err = fetch_content_pool_options(token, site, demo_id)
    if err or not options:
        if progress and err:
            progress(f"{site.upper()}: could not list content pools ({err}); using defaults.")
        return _pool_attempts(site)
    ordered = sorted(options, key=lambda row: _pool_rank(row[0], row[1], site))
    attempts: list[tuple[str, str | None]] = [(name, pool_id) for name, pool_id in ordered]
    if site == "syd":
        # SYD has always scheduled without a pool id, so keep that as the last resort.
        attempts.append(("SYD", None))
    return attempts


def _should_try_next_pool(message: str) -> bool:
    text = (message or "").lower()
    return any(
        phrase in text
        for phrase in (
            "schedule an instance of demo",
            "content pool",
            "not available in this pool",
            "unable to schedule",
            "resource",
            "0 available",
            "capacity",
        )
    )


def _should_try_next_slot(message: str) -> bool:
    text = (message or "").lower()
    return any(
        phrase in text
        for phrase in (
            "unable to schedule",
            "not available",
            "resource",
            "capacity",
            "conflict",
            "0 available",
        )
    )


def _conflict_payload(
    site_code: str,
    demo: str,
    pool_name: str,
    begin: datetime,
    end: datetime,
    alt: tuple[datetime, datetime] | None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "conflict": True,
        "site": site_code,
        "demoId": demo,
        "pool": pool_name,
        "requestedStart": _dcloud_timestamp(begin),
        "requestedStop": _dcloud_timestamp(end),
        "nextStart": _dcloud_timestamp(alt[0]) if alt else "",
        "nextStop": _dcloud_timestamp(alt[1]) if alt else "",
        "message": (
            f"Content could not be scheduled in {site_code.upper()} at the selected time."
        ),
    }


def find_schedule_conflict(
    token: str,
    site: str,
    demo_id: str,
    *,
    days: int = 1,
    start_at: str = "",
    stop_at: str = "",
) -> dict[str, Any] | None:
    """When the user wants a fixed time, check dCloud calendar before scheduling."""
    site_code = site.strip().lower()
    demo = str(demo_id).strip()
    if site_code not in KNOWN_SITES or not demo:
        return None
    window = resolve_schedule_window(days=days, start_at=start_at, stop_at=stop_at)
    if isinstance(window, str):
        return None
    begin, end = window
    duration = end - begin

    def _cal_end(from_when: datetime) -> datetime:
        return min(from_when + timedelta(days=14), from_when + duration + timedelta(days=7))

    saw_clear = False
    first_conflict: dict[str, Any] | None = None

    for pool_name, pool_id in _pool_attempts_for_demo(token, site_code, demo):
        cal_end = _cal_end(begin)
        timeslots, cal_err = fetch_content_calendar(
            token,
            site_code,
            demo,
            range_start=begin,
            range_end=cal_end,
            pool_id=pool_id,
        )
        if cal_err:
            saw_clear = True
            continue
        if not window_has_conflict(begin, end, timeslots):
            saw_clear = True
            break
        if first_conflict is None:
            alt = find_next_available_window(
                begin,
                duration,
                timeslots,
                search_end=cal_end,
            )
            if alt and alt[0] > begin + MAX_NEXT_SLOT_DELAY:
                alt = None
            first_conflict = _conflict_payload(site_code, demo, pool_name, begin, end, alt)

    if saw_clear:
        return None
    return first_conflict


def schedule_exported_session(
    token: str,
    site: str,
    demo_id: str,
    *,
    days: int = 1,
    start_at: str = "",
    stop_at: str = "",
    content_export: bool = True,
    auto_next_available: bool = True,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Schedule a session. content_export=True is bot `/sch ID,days,min,exp`; False is regular `/sch`."""
    site_code = site.strip().lower()
    demo = str(demo_id).strip()
    if site_code not in KNOWN_SITES:
        return {"ok": False, "message": f"Unknown datacenter {site}."}
    if not demo:
        return {"ok": False, "message": "Demo / content ID is required."}

    window = resolve_schedule_window(days=days, start_at=start_at, stop_at=stop_at)
    if isinstance(window, str):
        return {"ok": False, "site": site_code, "message": window}
    begin, end = window
    duration = end - begin
    kind = "exported" if content_export else "regular"

    url = f"{site_base(site_code)}/api/sessions/schedule"
    last_message = "Schedule failed."
    if progress:
        progress(
            f"{site_code.upper()}: scheduling {kind} session for demo {demo} "
            f"{begin.strftime('%Y-%m-%d %H:%M')}–{end.strftime('%Y-%m-%d %H:%M')} UTC "
            f"(contentExport={'true' if content_export else 'false'}; "
            "Dev Pool first, then Public/Core if needed)."
        )

    def _calendar_search_end(from_when: datetime) -> datetime:
        return min(from_when + timedelta(days=14), from_when + duration + timedelta(days=7))

    def _pick_window_for_pool(
        pool_name: str,
        pool_id: str | None,
        begin_at: datetime,
        end_at: datetime,
    ) -> tuple[datetime, datetime, bool] | None:
        if not auto_next_available:
            cal_end = _calendar_search_end(begin_at)
            timeslots, cal_err = fetch_content_calendar(
                token,
                site_code,
                demo,
                range_start=begin_at,
                range_end=cal_end,
                pool_id=pool_id,
            )
            if cal_err:
                return begin_at, end_at, False
            if window_has_conflict(begin_at, end_at, timeslots):
                return None
            return begin_at, end_at, False
        cal_end = _calendar_search_end(begin_at)
        timeslots, cal_err = fetch_content_calendar(
            token,
            site_code,
            demo,
            range_start=begin_at,
            range_end=cal_end,
            pool_id=pool_id,
        )
        if cal_err:
            if progress:
                progress(f"{site_code.upper()}: calendar check skipped ({cal_err}).")
            return begin_at, end_at, False
        if not window_has_conflict(begin_at, end_at, timeslots):
            return begin_at, end_at, False
        alt = find_next_available_window(
            begin_at,
            duration,
            timeslots,
            search_end=cal_end,
        )
        if alt and alt[0] > begin_at + MAX_NEXT_SLOT_DELAY:
            if progress:
                progress(
                    f"{site_code.upper()}: next {pool_name} slot starts more than "
                    "24 hours away; not scheduling it automatically."
                )
            return None
        if not alt:
            # A long session can never fit the 14-day lookahead, so asking dCloud
            # beats skipping the pool on a window we could not have found anyway.
            if duration > cal_end - begin_at:
                if progress:
                    progress(
                        f"{site_code.upper()}: {pool_name} calendar is busy but the session is "
                        f"longer than the {cal_end.strftime('%Y-%m-%d')} lookahead — asking dCloud anyway."
                    )
                return begin_at, end_at, False
            if progress:
                progress(
                    f"{site_code.upper()}: no open slot in calendar for {pool_name} "
                    f"before {cal_end.strftime('%Y-%m-%d %H:%M')} UTC."
                )
            return None
        new_begin, new_end = alt
        if progress:
            progress(
                f"{site_code.upper()}: resources busy at requested time — "
                f"using next slot {new_begin.strftime('%Y-%m-%d %H:%M')}–"
                f"{new_end.strftime('%Y-%m-%d %H:%M')} UTC ({pool_name})."
            )
        return new_begin, new_end, True

    def _post_schedule(
        start_ts: str,
        stop_ts: str,
        pool_name: str,
        pool_id: str | None,
    ) -> dict[str, Any]:
        nonlocal last_message
        if progress:
            progress(f"{site_code.upper()}: trying {pool_name}…")
        payload = _schedule_payload(
            demo, start_ts, stop_ts, pool_id=pool_id, content_export=content_export
        )
        try:
            response = _request("POST", url, token, json_body=payload, timeout=60)
        except requests.RequestException as exc:
            return {"ok": False, "message": str(exc)}
        body = _json_or_text(response)
        last_message = api_message(body) or f"HTTP {response.status_code}"
        success = isinstance(body, dict) and body.get("success") is True
        sessions = body.get("sessions") if isinstance(body, dict) else None
        if success and isinstance(sessions, list) and sessions:
            uid = str(sessions[0].get("uid") or "")
            first = sessions[0] if isinstance(sessions[0], dict) else {}
            return {
                "ok": True,
                "sessionId": uid,
                "viewUrl": session_view_url(site_code, uid, session=first) if uid else "",
                "message": last_message,
            }
        return {"ok": False, "message": last_message}

    last_conflict: dict[str, Any] | None = None
    resource_failure: tuple[str, str | None, str, str] | None = None
    pools = _pool_attempts_for_demo(token, site_code, demo, progress=progress)
    skipped: list[str] = []

    for pool_name, pool_id in pools:
        picked = _pick_window_for_pool(pool_name, pool_id, begin, end)
        if picked is None:
            cal_end = _calendar_search_end(begin)
            timeslots, cal_err = fetch_content_calendar(
                token,
                site_code,
                demo,
                range_start=begin,
                range_end=cal_end,
                pool_id=pool_id,
            )
            if not cal_err and window_has_conflict(begin, end, timeslots):
                alt = find_next_available_window(
                    begin,
                    duration,
                    timeslots,
                    search_end=cal_end,
                )
                if alt and alt[0] > begin + MAX_NEXT_SLOT_DELAY:
                    alt = None
                if last_conflict is None:
                    last_conflict = _conflict_payload(
                        site_code, demo, pool_name, begin, end, alt
                    )
                if progress:
                    progress(
                        f"{site_code.upper()}: {pool_name} busy at requested time — "
                        "trying next pool…"
                    )
            skipped.append(pool_name)
            continue
        begin_use, end_use, adjusted = picked
        start = _dcloud_timestamp(begin_use)
        stop = _dcloud_timestamp(end_use)

        result = _post_schedule(start, stop, pool_name, pool_id)
        if result.get("ok"):
            uid = result.get("sessionId") or ""
            if progress:
                progress(f"{site_code.upper()}: scheduled {kind} session {uid} in {pool_name}.")
            return {
                "ok": True,
                "site": site_code,
                "demoId": demo,
                "sessionId": uid,
                "pool": pool_name,
                "contentExport": content_export,
                "scheduleStart": start,
                "scheduleStop": stop,
                "adjusted": adjusted,
                "message": (
                    f"{kind.capitalize()} session {uid} scheduled in {pool_name}"
                    + (" (next available slot)." if adjusted else ".")
                ),
                "viewUrl": result.get("viewUrl") or "",
            }

        if progress:
            progress(f"{site_code.upper()}: {pool_name} not available ({result.get('message') or last_message}).")
        failed_message = str(result.get("message") or last_message)
        resource = unavailable_resource_name(failed_message)
        if resource:
            resource_failure = (pool_name, pool_id, resource, failed_message)

        if not auto_next_available and _should_try_next_slot(failed_message):
            cal_end = _calendar_search_end(begin_use)
            timeslots, cal_err = fetch_content_calendar(
                token,
                site_code,
                demo,
                range_start=begin_use,
                range_end=cal_end,
                pool_id=pool_id,
            )
            alt = None
            if not cal_err:
                alt = find_next_available_window(
                    begin_use + timedelta(minutes=1),
                    duration,
                    timeslots,
                    search_end=cal_end,
                ) if timeslots else None
                if alt and alt[0] > begin_use + MAX_NEXT_SLOT_DELAY:
                    alt = None
            if last_conflict is None:
                last_conflict = _conflict_payload(
                    site_code, demo, pool_name, begin, end, alt
                )
            continue

        if auto_next_available and _should_try_next_slot(result.get("message") or last_message):
            cal_end = _calendar_search_end(begin_use)
            timeslots, _cal_err = fetch_content_calendar(
                token,
                site_code,
                demo,
                range_start=begin_use,
                range_end=cal_end,
                pool_id=pool_id,
            )
            retry_from = begin_use + timedelta(minutes=1)
            alt = find_next_available_window(
                retry_from,
                duration,
                timeslots,
                search_end=cal_end,
            ) if timeslots else None
            if alt and alt[0] > begin_use + MAX_NEXT_SLOT_DELAY:
                alt = None
            if alt and alt[0] > begin_use:
                retry_begin, retry_end = alt
                retry_start = _dcloud_timestamp(retry_begin)
                retry_stop = _dcloud_timestamp(retry_end)
                if progress:
                    progress(
                        f"{site_code.upper()}: retrying {pool_name} at "
                        f"{retry_begin.strftime('%Y-%m-%d %H:%M')} UTC…"
                    )
                retry = _post_schedule(retry_start, retry_stop, pool_name, pool_id)
                if retry.get("ok"):
                    uid = retry.get("sessionId") or ""
                    if progress:
                        progress(f"{site_code.upper()}: scheduled {kind} session {uid} in {pool_name}.")
                    return {
                        "ok": True,
                        "site": site_code,
                        "demoId": demo,
                        "sessionId": uid,
                        "pool": pool_name,
                        "contentExport": content_export,
                        "scheduleStart": retry_start,
                        "scheduleStop": retry_stop,
                        "adjusted": True,
                        "message": (
                            f"{kind.capitalize()} session {uid} scheduled in {pool_name} "
                            "(next available slot)."
                        ),
                        "viewUrl": retry.get("viewUrl") or "",
                    }
                last_message = retry.get("message") or last_message

        if not _should_try_next_pool(result.get("message") or last_message):
            break

    if skipped and len(skipped) == len(pools):
        # Nothing was ever sent to dCloud, so last_message would be misleading.
        if last_conflict:
            last_conflict["message"] = (
                "Resources are busy for the requested window. "
                "No suitable opening starts within the next 24 hours."
            )
            return last_conflict
        last_message = (
            f"{site_code.upper()} calendar shows no open window for "
            f"{', '.join(skipped)} in the requested time range."
        )

    if resource_failure:
        pool_name, pool_id, resource, failed_message = resource_failure
        shorter = find_shorter_schedule_option(
            token,
            site_code,
            demo,
            desired_start=begin,
            requested_duration=duration,
            pool_id=pool_id,
        )
        result = _conflict_payload(
            site_code,
            demo,
            pool_name,
            begin,
            end,
            (shorter[0], shorter[1]) if shorter else None,
        )
        result.update(
            {
                "resource": resource,
                "reason": failed_message,
                "suggestedDays": shorter[2] if shorter else 0,
                "message": (
                    f"{resource} has no available capacity for this session. "
                    "No suitable slot can start within the next 24 hours."
                    if not shorter
                    else
                    f"{resource} has no capacity for the requested duration. "
                    f"A {shorter[2]}-day session can start within the next 24 hours."
                ),
            }
        )
        return result

    if last_conflict:
        return last_conflict

    return {
        "ok": False,
        "site": site_code,
        "demoId": demo,
        "contentExport": content_export,
        "pools": [name for name, _pool_id in pools],
        "message": last_message,
    }


def match_selected_vms(
    session_vms: list[dict[str, Any]],
    selected: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    by_name = {vm["name"].strip().lower(): vm for vm in session_vms if vm.get("name")}
    by_display = {
        vm["displayName"].strip().lower(): vm
        for vm in session_vms
        if vm.get("displayName")
    }
    by_short = {
        vm["shortName"].strip().lower(): vm
        for vm in session_vms
        if vm.get("shortName")
    }
    by_mor = {vm["mor"]: vm for vm in session_vms if vm.get("mor")}
    by_uid = {vm["uid"]: vm for vm in session_vms if vm.get("uid")}
    matched: list[dict[str, Any]] = []
    missing: list[str] = []
    seen: set[str] = set()
    for sel in selected:
        name = str(sel.get("name") or sel.get("displayName") or "").strip()
        short = str(sel.get("shortName") or "").strip()
        mor = str(sel.get("mor") or "").strip()
        uid = str(sel.get("uid") or "").strip()
        hit = None
        if name and name.lower() in by_display:
            hit = by_display[name.lower()]
        elif name and name.lower() in by_name:
            hit = by_name[name.lower()]
        elif short and short.lower() in by_short:
            hit = by_short[short.lower()]
        elif short and short.lower() in by_name:
            hit = by_name[short.lower()]
        elif mor and mor in by_mor:
            hit = by_mor[mor]
        elif uid and uid in by_uid:
            hit = by_uid[uid]
        if not hit:
            missing.append(name or short or mor or uid or "?")
            continue
        key = hit.get("mor") or hit.get("uid") or hit["name"]
        if key in seen:
            continue
        seen.add(key)
        matched.append(hit)
    return matched, missing


def tag_selected_vms(
    session_vms: list[dict[str, Any]],
    selected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep the full session VM list, marking which ones were checked at schedule time."""
    tagged = [dict(vm) for vm in session_vms]
    if not tagged:
        return tagged
    pool = list(selected or [])
    if not pool:
        for vm in tagged:
            vm["selected"] = False
        return tagged
    matched, _ = match_selected_vms(tagged, pool)
    keys = {
        (vm.get("mor") or "", vm.get("uid") or "", str(vm.get("name") or "").strip().lower())
        for vm in matched
    }
    for vm in tagged:
        key = (vm.get("mor") or "", vm.get("uid") or "", str(vm.get("name") or "").strip().lower())
        vm["selected"] = key in keys
    return tagged


def vm_action(
    token: str,
    site: str,
    session_id: str,
    vm: dict[str, Any],
    action: str,
) -> dict[str, Any]:
    vmid = str(vm.get("mor") or vm.get("uid") or "")
    name = str(vm.get("name") or vmid)
    if not vmid:
        return {"ok": False, "name": name, "message": "VM is missing mor/uid."}
    url = f"{site_base(site)}/api/sessions/{session_id}/servers/{vmid}/action"
    try:
        response = _request("PUT", url, token, json_body={"action": action})
    except requests.RequestException as exc:
        return {"ok": False, "name": name, "mor": vmid, "message": str(exc)}
    body = _json_or_text(response)
    message = api_message(body) or f"HTTP {response.status_code}"
    ok = response.status_code < 400
    if isinstance(body, dict) and "success" in body:
        ok = body.get("success") is True
    return {"ok": ok, "name": name, "mor": vmid, "uid": vm.get("uid") or "", "message": message}


def _vm_name_blob(vm: dict[str, Any]) -> str:
    blob = " ".join(
        str(vm.get(key) or "") for key in ("name", "displayName", "shortName")
    ).lower()
    return blob.replace("-", "").replace("_", "").replace(" ", "")


def vm_needs_hard_power_off(vm: dict[str, Any]) -> bool:
    """vCUBE has no working guest shutdown — dCloud accepts the call, then the VM reboots."""
    return "vcube" in _vm_name_blob(vm)


# Cisco UC guests often take several minutes to halt. A short "then power off
# whoever is left" would yank CUCM mid-shutdown. Name match is conservative.
_SLOW_GUEST_SHUTDOWN_MARKERS = (
    "cucm",
    "ucmpub",
    "ucmsub",
    "unity",
    "imp",
    "imandp",
    "presence",
    "uccx",
    "finesse",
    "cvp",
    "pcce",
    "ucce",
    "expressway",
    "meetingserver",
    "cer",
)


def vm_is_slow_guest_shutdown(vm: dict[str, Any]) -> bool:
    if vm_needs_hard_power_off(vm):
        return False
    compact = _vm_name_blob(vm)
    if "cuc" in compact:
        return True
    return any(marker in compact for marker in _SLOW_GUEST_SHUTDOWN_MARKERS)


def power_on_vms(
    token: str,
    site: str,
    session_id: str,
    vms: list[dict[str, Any]],
    progress: Progress | None = None,
) -> list[dict[str, Any]]:
    results = []
    for vm in vms:
        if progress:
            progress(f"{site.upper()}: powering on {vm.get('name') or vm.get('mor')}…")
        results.append(vm_action(token, site, session_id, vm, "vmPowerOn"))
    return results


def guest_shutdown_vms(
    token: str,
    site: str,
    session_id: str,
    vms: list[dict[str, Any]],
    progress: Progress | None = None,
    *,
    fallback_power_off: bool = True,
) -> list[dict[str, Any]]:
    """Fire guest-shutdown requests; do not wait for VMs to power off.

    vCUBE has no guest OS shutdown — the API call is accepted, then the VM
    restarts. Power those off instead. Other VMs still fall back to hard
    power-off if guest shutdown is rejected.
    """
    results = []
    for vm in vms:
        name = str(vm.get("name") or vm.get("mor") or "VM")
        if vm_needs_hard_power_off(vm):
            if progress:
                progress(
                    f"{site.upper()}: {name} has no guest shutdown (vCUBE) — powering off…"
                )
            hard = vm_action(token, site, session_id, vm, "vmPowerOff")
            results.append(
                {
                    **hard,
                    "name": name,
                    "forcedPowerOff": True,
                    "reason": "vcube",
                }
            )
            if progress:
                if hard.get("ok"):
                    progress(f"{site.upper()}: power off {name}: {hard.get('message') or 'accepted'}")
                else:
                    progress(
                        f"{site.upper()}: power off failed for {name} — continuing to save anyway."
                    )
            continue
        if progress:
            progress(f"{site.upper()}: guest shutdown {name}…")
        result = vm_action(token, site, session_id, vm, "guestShutdown")
        if result.get("ok"):
            if progress:
                progress(f"{site.upper()}: guest shutdown {name}: {result.get('message') or 'accepted'}")
        elif fallback_power_off:
            msg = str(result.get("message") or "failed")
            if progress:
                progress(
                    f"{site.upper()}: guest shutdown failed for {name} ({msg}) — trying power off…"
                )
            hard = vm_action(token, site, session_id, vm, "vmPowerOff")
            result = {
                **result,
                "fallback": "vmPowerOff",
                "fallbackOk": hard.get("ok"),
                "fallbackMessage": hard.get("message"),
            }
            if hard.get("ok"):
                if progress:
                    progress(f"{site.upper()}: power off {name}: {hard.get('message') or 'accepted'}")
            elif progress:
                progress(f"{site.upper()}: power off also failed for {name} — continuing to save anyway.")
        else:
            if progress:
                progress(f"{site.upper()}: guest shutdown failed for {name} — continuing to save anyway.")
        results.append(result)
    return results


def list_dashboard_sessions(token: str, site: str) -> tuple[list[dict[str, Any]], str | None]:
    """GET /api/sessions?expand=sharedWith — same list the bot /ms command uses."""
    site_code = (site or "").strip().lower()
    if site_code not in KNOWN_SITES:
        return [], "Datacenter must be SJC, RTP, LON, SNG, or SYD."
    url = f"{site_base(site_code)}/api/sessions?expand=sharedWith"
    try:
        response = _request("GET", url, token)
    except requests.RequestException as exc:
        return [], str(exc)
    if response.status_code == 401:
        return [], (
            "dCloud token was rejected (401). Use Sign in to dCloud at the top of the page "
            "to log in or import from browser, then try again."
        )
    if response.status_code >= 400:
        return [], api_message(_json_or_text(response)) or f"HTTP {response.status_code}"
    body = _json_or_text(response)
    if not isinstance(body, dict):
        return [], "Unexpected sessions response."
    rows = body.get("content") or []
    if not isinstance(rows, list):
        return [], "Unexpected sessions response."
    out: list[dict[str, Any]] = []
    for session in rows:
        if not isinstance(session, dict):
            continue
        sid = str(session.get("uid") or session.get("id") or "").strip()
        if not sid:
            continue
        status = session.get("status")
        out.append(
            {
                "site": site_code,
                "sessionId": sid,
                "name": str(session.get("name") or "").strip(),
                "status": format_status(status),
                "rawStatus": status,
                "active": is_active_status(status),
                "demoId": str(session.get("demoId") or session.get("parentId") or "").strip(),
                "viewUrl": session_view_url(site_code, sid, session=session),
            }
        )
    return out, None


def list_dashboard_sessions_all_sites(token: str) -> dict[str, Any]:
    sessions: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(SITES)) as pool:
        futures = {
            pool.submit(list_dashboard_sessions, token, site): site for site in SITES
        }
        for future in as_completed(futures):
            site = futures[future]
            rows, err = future.result()
            if err:
                errors[site] = err
            sessions.extend(rows)
    order = {site: index for index, site in enumerate(SITES)}
    sessions.sort(key=lambda row: (order.get(row.get("site") or "", 99), row.get("sessionId") or ""))
    return {"sessions": sessions, "errors": errors}


def _monitor_session_name(session: dict[str, Any]) -> str:
    for obj in (
        session,
        session.get("sessionDetails"),
        session.get("session"),
        session.get("demo"),
    ):
        if not isinstance(obj, dict):
            continue
        for key in ("name", "demoName", "parentDemoName"):
            value = str(obj.get(key) or "").strip()
            if value:
                return value
    return ""


def resolve_monitor_sessions(
    token: str,
    site: str,
    identifier: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """Resolve a session ID directly, or an admin-visible demo/content ID to live sessions."""
    site_code = (site or "").strip().lower()
    wanted = str(identifier or "").strip()
    if site_code not in KNOWN_SITES:
        return [], "Datacenter must be SJC, RTP, LON, SNG, or SYD."
    if not wanted:
        return [], "Enter a demo ID or session ID."

    direct, direct_err = fetch_session(token, site_code, wanted, expand="all")
    if direct:
        return [
            {
                "site": site_code,
                "sessionId": wanted,
                "demoId": str(direct.get("demoId") or direct.get("parentId") or "").strip(),
                "name": _monitor_session_name(direct),
                "status": format_status(direct.get("status")),
            }
        ], None
    if direct_err and is_auth_error(direct_err):
        return [], direct_err

    url = f"{site_base(site_code)}/api/admin/sessions"
    try:
        response = _request("GET", url, token, timeout=60)
    except requests.RequestException as exc:
        return [], str(exc)
    if response.status_code == 401:
        return [], (
            "dCloud rejected the token. Use Sign in to dCloud at the top of the page to "
            "log in or import a fresh token."
        )
    if response.status_code in {403, 404}:
        return [], "Admin session lookup is not available for this dCloud account."
    if response.status_code >= 400:
        return [], api_message(_json_or_text(response)) or f"HTTP {response.status_code}"
    body = _json_or_text(response)
    rows = body.get("content") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return [], "Unexpected admin sessions response."

    live_statuses = {
        "1", "2", "4", "5", "12",
        "scheduled", "starting", "active", "stopping", "saving",
        "waiting", "queued", "provisioning",
    }
    matches: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("uid") or row.get("id") or row.get("sessionId") or "").strip()
        if not sid or sid in seen:
            continue
        demo_ids = {
            str(row.get(key) or "").strip()
            for key in ("demoId", "parentId", "activeId", "contentId", "demoUid")
        }
        nested = row.get("demo")
        if isinstance(nested, dict):
            demo_ids.update(
                str(nested.get(key) or "").strip()
                for key in ("uid", "id", "demoId", "parentId")
            )
        if wanted != sid and wanted not in demo_ids:
            continue
        status = row.get("status")
        status_key = _status_text(status).lower()
        if status_key and status_key not in live_statuses:
            continue
        seen.add(sid)
        matches.append(
            {
                "site": site_code,
                "sessionId": sid,
                "demoId": next((value for value in demo_ids if value), ""),
                "name": _monitor_session_name(row),
                "status": format_status(status),
            }
        )
    return matches, None


def resolve_extended_stop(
    *,
    current_stop: str,
    session_start: str = "",
    days: int = 1,
) -> tuple[str, str | None]:
    """Push session stop forward by days; returns (iso_stop, error)."""
    days = max(1, int(days or 1))
    stop = parse_schedule_datetime(current_stop)
    if stop is None:
        return "", "Could not read the current session end time."
    new_stop = stop + timedelta(days=days)
    start = parse_schedule_datetime(session_start)
    if start is not None and new_stop <= start:
        return "", "Extended stop must be after session start."
    return _dcloud_timestamp(new_stop), None


def resolve_extended_stop_by_minutes(
    *,
    current_stop: str,
    extra_minutes: int,
    session_start: str = "",
) -> tuple[str, str | None]:
    extra = int(extra_minutes or 0)
    if extra < 30:
        return "", "Extend by at least 30 minutes."
    stop = parse_schedule_datetime(current_stop)
    if stop is None:
        return "", "Could not read the current session end time."
    new_stop = stop + timedelta(minutes=extra)
    start = parse_schedule_datetime(session_start)
    if start is not None and new_stop <= start:
        return "", "Extended stop must be after session start."
    return _dcloud_timestamp(new_stop), None


def extend_is_capacity_blocked(message: str) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    return (
        "fully booked" in text
        or "resources are fully booked" in text
        or "booked out after" in text
        or ("resource" in text and "unavailable" in text)
    )


def _floor_minutes(when: datetime, step: int = 5) -> datetime:
    when = when.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return when.replace(minute=when.minute - (when.minute % step))


def shorter_extend_stops(current_stop: str, requested_stop: str) -> list[str]:
    """Later-to-earlier stops to try when the requested extend is booked.

    dCloud often has room for a few hours even when the next full day is full.
    """
    current = parse_schedule_datetime(current_stop)
    requested = parse_schedule_datetime(requested_stop)
    if current is None or requested is None or requested <= current:
        return []
    extra = requested - current
    floor = current + timedelta(minutes=30)
    if requested <= floor:
        return []
    seen: set[str] = set()
    out: list[str] = []

    def add(when: datetime) -> None:
        when = _floor_minutes(when, 5)
        if when < floor or when >= requested:
            return
        stamp = _dcloud_timestamp(when)
        if stamp in seen:
            return
        seen.add(stamp)
        out.append(stamp)

    for frac in (0.75, 0.5, 0.25):
        add(current + extra * frac)
    for hours in (12, 6, 3, 2, 1):
        add(current + timedelta(hours=hours))
    add(current + timedelta(minutes=30))
    return out


def extend_session(
    token: str,
    site: str,
    session_id: str,
    *,
    stop_at: str,
) -> dict[str, Any]:
    """PUT /api/sessions/{id} with a new stop time — same as the dCloud UI extend."""
    sid = (session_id or "").strip()
    stop = (stop_at or "").strip()
    if not sid:
        return {"ok": False, "message": "Session ID is required."}
    if not stop:
        return {"ok": False, "message": "Stop time is required."}
    url = f"{site_base(site)}/api/sessions/{sid}"
    try:
        response = _request("PUT", url, token, json_body={"stop": stop}, timeout=60)
    except requests.RequestException as exc:
        return {"ok": False, "sessionId": sid, "message": str(exc)}
    body = _json_or_text(response)
    if response.status_code == 404:
        return {"ok": False, "sessionId": sid, "message": f"Session {sid} not found in {site.upper()}."}
    message = api_message(body) or f"HTTP {response.status_code}"
    ok = response.status_code < 400
    if isinstance(body, dict) and "success" in body:
        ok = body.get("success") is True
    session = body.get("session") if isinstance(body, dict) else None
    new_stop = ""
    if isinstance(session, dict):
        new_stop = str(session.get("stop") or "").strip()
    return {
        "ok": ok,
        "sessionId": sid,
        "message": message if message else ("Session extended." if ok else "Extend failed."),
        "stop": new_stop,
        "session": session if isinstance(session, dict) else {},
    }


def probe_max_extend_stop(
    token: str,
    site: str,
    session_id: str,
    *,
    requested_stop: str,
    current_stop: str = "",
    put: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Try the requested stop, then find the farthest shorter stop that dCloud will take.

    A successful shorter probe is reverted so nothing is kept until the user confirms.
    """
    sid = (session_id or "").strip()
    put_fn = put or (lambda stop: extend_session(token, site, sid, stop_at=stop))
    requested = (requested_stop or "").strip()
    original = (current_stop or "").strip()
    first = put_fn(requested)
    if first.get("ok"):
        return {
            "ok": True,
            "applied": True,
            "offer": False,
            "sessionId": sid,
            "stop": str(first.get("stop") or requested).strip(),
            "requested_stop": requested,
            "message": first.get("message") or "Session extended.",
        }
    if not extend_is_capacity_blocked(first.get("message") or ""):
        return {
            "ok": False,
            "applied": False,
            "offer": False,
            "sessionId": sid,
            "requested_stop": requested,
            "message": first.get("message") or "Extend failed.",
        }

    current = parse_schedule_datetime(original)
    want = parse_schedule_datetime(requested)
    if current is None or want is None or want <= current + timedelta(minutes=30):
        return {
            "ok": False,
            "applied": False,
            "offer": False,
            "sessionId": sid,
            "requested_stop": requested,
            "message": first.get("message") or "Resources are fully booked after the session.",
        }

    lo = current + timedelta(minutes=30)
    hi = want
    best_stamp = ""
    last = first

    def revert() -> dict[str, Any]:
        return put_fn(original)

    for _ in range(14):
        if hi - lo < timedelta(minutes=5):
            break
        mid = _floor_minutes(lo + (hi - lo) / 2, 5)
        if mid <= lo:
            mid = _floor_minutes(lo + timedelta(minutes=5), 5)
        if mid >= hi:
            break
        stamp = _dcloud_timestamp(mid)
        result = put_fn(stamp)
        last = result
        if result.get("ok"):
            best_stamp = str(result.get("stop") or stamp).strip()
            undone = revert()
            if not undone.get("ok"):
                return {
                    "ok": True,
                    "applied": True,
                    "offer": False,
                    "sessionId": sid,
                    "stop": best_stamp,
                    "requested_stop": requested,
                    "message": (
                        f"Extended until {best_stamp} (requested {requested}; "
                        "could not restore the original end after probing)."
                    ),
                }
            lo = mid
            continue
        if extend_is_capacity_blocked(result.get("message") or ""):
            hi = mid
            continue
        return {
            "ok": False,
            "applied": False,
            "offer": False,
            "sessionId": sid,
            "requested_stop": requested,
            "message": result.get("message") or "Extend failed.",
        }

    if not best_stamp:
        floor_stamp = _dcloud_timestamp(_floor_minutes(lo, 5))
        if parse_schedule_datetime(floor_stamp) and parse_schedule_datetime(floor_stamp) < want:
            result = put_fn(floor_stamp)
            last = result
            if result.get("ok"):
                best_stamp = str(result.get("stop") or floor_stamp).strip()
                undone = revert()
                if not undone.get("ok"):
                    return {
                        "ok": True,
                        "applied": True,
                        "offer": False,
                        "sessionId": sid,
                        "stop": best_stamp,
                        "requested_stop": requested,
                        "message": (
                            f"Extended until {best_stamp} (requested {requested}; "
                            "could not restore the original end after probing)."
                        ),
                    }

    if best_stamp:
        return {
            "ok": False,
            "applied": False,
            "offer": True,
            "sessionId": sid,
            "suggested_stop": best_stamp,
            "requested_stop": requested,
            "current_stop": original,
            "message": (
                "Resources are fully booked after the session. "
                f"The farthest you can go is {best_stamp}."
            ),
        }
    return {
        "ok": False,
        "applied": False,
        "offer": False,
        "sessionId": sid,
        "requested_stop": requested,
        "message": last.get("message") or first.get("message") or "Resources are fully booked after the session.",
    }


def update_session_schedule(
    token: str,
    site: str,
    session_id: str,
    *,
    name: str = "",
    start_at: str = "",
    stop_at: str = "",
) -> dict[str, Any]:
    """PUT /api/sessions/{id} with the fields dCloud's Edit form changes."""
    site_code = (site or "").strip().lower()
    sid = (session_id or "").strip()
    if site_code not in KNOWN_SITES:
        return {"ok": False, "message": "Datacenter must be SJC, RTP, LON, SNG, or SYD."}
    if not sid:
        return {"ok": False, "message": "Session ID is required."}
    body: dict[str, Any] = {}
    new_name = (name or "").strip()
    if new_name:
        if len(new_name) > 255:
            return {"ok": False, "message": "Session name must be 255 characters or fewer."}
        body["name"] = new_name
    start = parse_schedule_datetime(start_at) if start_at else None
    stop = parse_schedule_datetime(stop_at) if stop_at else None
    if start_at and start is None:
        return {"ok": False, "message": "Start time could not be read."}
    if stop_at and stop is None:
        return {"ok": False, "message": "End time could not be read."}
    if start and stop and stop <= start:
        return {"ok": False, "message": "End time must be after the start time."}
    if start:
        body["start"] = _dcloud_timestamp(start)
    if stop:
        body["stop"] = _dcloud_timestamp(stop)
    if not body:
        return {"ok": False, "message": "Nothing to update."}
    url = f"{site_base(site_code)}/api/sessions/{sid}"
    try:
        response = _request("PUT", url, token, json_body=body, timeout=60)
    except requests.RequestException as exc:
        return {"ok": False, "sessionId": sid, "message": describe_request_error(exc, "dCloud")}
    parsed = _json_or_text(response)
    if response.status_code == 404:
        return {"ok": False, "sessionId": sid, "message": f"Session {sid} not found in {site_code.upper()}."}
    ok = response.status_code < 400
    if isinstance(parsed, dict) and "success" in parsed:
        ok = parsed.get("success") is True
    session = parsed.get("session") if isinstance(parsed, dict) else None
    message = api_message(parsed) or f"HTTP {response.status_code}"
    return {
        "ok": ok,
        "sessionId": sid,
        "message": message if message else ("Session updated." if ok else "Update failed."),
        "session": session if isinstance(session, dict) else {},
    }


def fetch_session_log(token: str, site: str, session_id: str) -> tuple[str, str | None]:
    """GET /api/admin/logs/sessions/{id} — the HTML blob behind the dashboard Logs button."""
    site_code = (site or "").strip().lower()
    sid = str(session_id or "").strip()
    if site_code not in KNOWN_SITES or not sid:
        return "", "Datacenter and session ID are required."
    url = f"{site_base(site_code)}/api/admin/logs/sessions/{quote(sid)}"
    try:
        response = _request("GET", url, token, timeout=90)
    except requests.RequestException as exc:
        return "", describe_request_error(exc, "dCloud")
    if response.status_code == 401:
        return "", "dCloud token was rejected (401)."
    if response.status_code == 404:
        return "", f"No log is available for session {sid}."
    if response.status_code >= 400:
        return "", api_message(_json_or_text(response)) or f"HTTP {response.status_code}"
    # dCloud wraps each line in styled divs; keep the text so the UI never renders
    # markup that came back from the API.
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", response.text or "")
    text = re.sub(r"(?i)</div>|<br\s*/?>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = unescape(text)
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(line for line in lines if line.strip()), None


def update_session_name(
    token: str,
    site: str,
    session_id: str,
    name: str,
) -> dict[str, Any]:
    """PUT /api/sessions/{id} with a new name — same as the dCloud dashboard rename."""
    site_code = (site or "").strip().lower()
    sid = (session_id or "").strip()
    new_name = (name or "").strip()
    if site_code not in KNOWN_SITES:
        return {"ok": False, "message": "Datacenter must be SJC, RTP, LON, SNG, or SYD."}
    if not sid:
        return {"ok": False, "message": "Session ID is required."}
    if not new_name:
        return {"ok": False, "message": "Session name is required."}
    if len(new_name) > 255:
        return {"ok": False, "message": "Session name must be 255 characters or fewer."}
    url = f"{site_base(site_code)}/api/sessions/{sid}"
    try:
        response = _request("PUT", url, token, json_body={"name": new_name}, timeout=60)
    except requests.RequestException as exc:
        return {"ok": False, "sessionId": sid, "message": str(exc)}
    body = _json_or_text(response)
    if response.status_code == 404:
        return {"ok": False, "sessionId": sid, "message": f"Session {sid} not found in {site_code.upper()}."}
    message = api_message(body) or f"HTTP {response.status_code}"
    ok = response.status_code < 400
    if isinstance(body, dict) and "success" in body:
        ok = body.get("success") is True
    session = body.get("session") if isinstance(body, dict) else None
    resolved_name = new_name
    if isinstance(session, dict):
        resolved_name = str(session.get("name") or new_name).strip() or new_name
    return {
        "ok": ok,
        "sessionId": sid,
        "name": resolved_name,
        "message": message if message else ("Session renamed." if ok else "Rename failed."),
        "session": session if isinstance(session, dict) else {},
    }


def _looks_like_permission_error(status_code: int, message: str) -> bool:
    """dCloud answers a session you do not own with 403, or a 400 carrying this text."""
    if status_code in {401, 403}:
        return True
    lowered = (message or "").lower()
    return "permission" in lowered or "has either been removed" in lowered


def _session_action(
    token: str,
    site: str,
    session_id: str,
    action: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[bool, Any, int, str, list[str]]:
    """PUT a session action, falling back to the admin route for someone else's session.

    The plain /api/sessions route only acts on sessions you own — an admin acting on
    another user's session gets "removed or you do not have the permission" from it,
    even though the same admin can read that session. Returns the attempted paths so
    a failure can say which ones were tried.
    """
    sid = (session_id or "").strip()
    paths = [f"/api/sessions/{sid}/{action}", f"/api/admin/sessions/{sid}/{action}"]
    tried: list[str] = []
    ok = False
    body: Any = ""
    status = 0
    message = ""
    for path in paths:
        tried.append(path)
        try:
            response = _request("PUT", f"{site_base(site)}{path}", token, timeout=timeout)
        except requests.RequestException as exc:
            ok, body, status, message = False, "", 0, str(exc)
            continue
        body = _json_or_text(response)
        status = response.status_code
        ok = status < 400
        if isinstance(body, dict) and "success" in body:
            ok = body.get("success") is True
        message = api_message(body)
        if ok or not _looks_like_permission_error(status, message):
            break
    return ok, body, status, message, tried


def end_session(token: str, site: str, session_id: str) -> dict[str, Any]:
    """PUT /api/sessions/{id}/end — same as the bot /end command (no save)."""
    sid = (session_id or "").strip()
    if not sid:
        return {"ok": False, "message": "Session ID is required."}
    ok, _body, status, detail, tried = _session_action(token, site, sid, "end")
    if status == 404 and not ok:
        return {"ok": False, "sessionId": sid, "message": f"Session {sid} not found in {site.upper()}."}
    message = detail or f"HTTP {status}"
    return {
        "ok": ok,
        "sessionId": sid,
        "triedPaths": tried,
        "message": message if message else ("Session ended." if ok else "End session failed."),
    }


def reset_session(token: str, site: str, session_id: str) -> dict[str, Any]:
    """PUT /api/sessions/{id}/reset — the dashboard Reset button. Keeps the same demo and session ID."""
    sid = (session_id or "").strip()
    if not sid:
        return {"ok": False, "message": "Session ID is required."}
    ok, body, status, detail, tried = _session_action(token, site, sid, "reset", timeout=60)
    if status == 404 and not ok:
        return {"ok": False, "sessionId": sid, "message": f"Session {sid} not found in {site.upper()}."}
    # dCloud answers a good reset with `"message": []`, so fall back to our own wording.
    message = detail or ("Reset requested." if ok else f"Reset failed (HTTP {status}).")
    session = body.get("session") if isinstance(body, dict) else None
    return {
        "ok": ok,
        "sessionId": sid,
        "session": session if isinstance(session, dict) else {},
        "triedPaths": tried,
        "message": message,
    }


def wait_until_active(
    token: str,
    site: str,
    session_id: str,
    *,
    timeout_seconds: int = 5400,
    poll_seconds: int = 20,
    progress: Progress | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_session: Callable[[dict[str, Any]], None] | None = None,
    on_status: Callable[[str, dict[str, Any]], None] | None = None,
    get_token: GetToken | None = None,
    refresh_auth: RefreshAuth | None = None,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_status = ""
    last_details: dict[str, Any] | None = None
    current_token = token
    while time.time() < deadline:
        if should_stop and should_stop():
            return {"ok": False, "status": last_status, "message": "Stopped by user."}
        if get_token:
            current_token = get_token() or current_token

        public_status, _pub_err = check_public_session_status(site, session_id)
        details, err = fetch_session(current_token, site, session_id, expand="server")
        if is_auth_error(err):
            if refresh_auth:
                new_token, refresh_err = refresh_auth(current_token)
                if refresh_err:
                    return {"ok": False, "status": last_status, "message": refresh_err}
                current_token = new_token
                continue
            return {"ok": False, "status": last_status, "message": err}
        if details:
            last_details = details
            if on_session:
                on_session(details)
        numeric = ""
        if details:
            numeric = _status_text(details.get("status") or details.get("sessionStatus"))
        elif err:
            if progress:
                progress(f"{site.upper()}: {err}")

        last_status = format_status(numeric, public_status)
        if is_active_status(public_status) or is_active_status(numeric):
            if progress:
                progress(f"{site.upper()}: session {session_id} is Active ({last_status}).")
            return {
                "ok": True,
                "status": last_status,
                "session": details or last_details or {},
                "message": "Active",
                "viewUrl": session_view_url(site, session_id, session=details or last_details),
            }
        if is_saved_status(public_status) or is_saved_status(numeric):
            if progress:
                progress(f"{site.upper()}: session {session_id} already saved ({last_status}).")
            return {
                "ok": False,
                "saved": True,
                "status": last_status,
                "session": details or last_details or {},
                "message": f"Session already saved ({last_status}).",
            }
        if is_failed_status(public_status) or is_failed_status(numeric):
            return {
                "ok": False,
                "status": last_status,
                "session": details or last_details or {},
                "message": f"Session ended in {last_status}.",
            }
        if on_status:
            on_status(last_status, details or last_details or {})
        if progress:
            progress(f"{site.upper()}: waiting for Active (currently {last_status})…")
        time.sleep(poll_seconds)
    return {
        "ok": False,
        "status": last_status,
        "message": f"Timed out waiting for Active (last status: {last_status or 'unknown'}).",
    }


def wait_for_power_state(
    token: str,
    site: str,
    session_id: str,
    vms: list[dict[str, Any]],
    *,
    want_on: bool,
    timeout_seconds: int = 600,
    poll_seconds: int = 15,
    progress: Progress | None = None,
    should_stop: Callable[[], bool] | None = None,
    get_token: GetToken | None = None,
    refresh_auth: RefreshAuth | None = None,
    on_wait: Callable[[list[dict[str, Any]], list[dict[str, Any]]], None] | None = None,
) -> dict[str, Any]:
    wanted_keys: set[str] = set()
    wanted_names: set[str] = set()
    for vm in vms:
        for field in ("mor", "uid", "name"):
            value = str(vm.get(field) or "").strip().lower()
            if value:
                wanted_keys.add(value)
        name = str(vm.get("name") or "").strip().lower()
        if name:
            wanted_names.add(name)
    deadline = time.time() + timeout_seconds
    label = "powered on" if want_on else "powered off"
    last_current: list[dict[str, Any]] = []
    logged_tbv3 = False
    current_token = token
    while time.time() < deadline:
        if should_stop and should_stop():
            return {"ok": False, "message": "Stopped by user."}
        if get_token:
            current_token = get_token() or current_token
        current, details, err = list_session_vms(current_token, site, session_id)
        if is_auth_error(err):
            if refresh_auth:
                new_token, refresh_err = refresh_auth(current_token)
                if refresh_err:
                    return {"ok": False, "message": refresh_err, "vms": last_current}
                current_token = new_token
                continue
            return {"ok": False, "message": err or "dCloud token expired (401).", "vms": last_current}
        if err:
            if progress:
                progress(f"{site.upper()}: {err}")
            time.sleep(poll_seconds)
            continue
        selected = []
        for vm in current:
            key = (vm.get("mor") or vm.get("uid") or vm.get("name") or "").strip().lower()
            if key not in wanted_keys and vm["name"].strip().lower() not in wanted_names:
                continue
            selected.append(vm)
        if not selected:
            if progress:
                progress(f"{site.upper()}: could not match target VMs while waiting for power state.")
            return {"ok": False, "message": "Could not match VMs for power wait.", "vms": last_current}
        selected, power_err = apply_tbv3_power_states(current_token, site, session_id, selected, details)
        if is_auth_error(power_err):
            if refresh_auth:
                new_token, refresh_err = refresh_auth(current_token)
                if refresh_err:
                    return {"ok": False, "message": refresh_err, "vms": selected}
                current_token = new_token
                continue
            return {"ok": False, "message": power_err, "vms": selected}
        last_current = selected
        if not logged_tbv3:
            logged_tbv3 = True
            topo = extract_topology_uid(details)
            if progress and not topo:
                progress(
                    f"{site.upper()}: session has no topologyVersionUid; "
                    "tbv3 vm-status cannot be used."
                )
            elif progress and all(not str(vm.get("powerState") or "").strip() for vm in selected):
                progress(
                    f"{site.upper()}: tbv3 vm-status returned no powerState yet "
                    f"(versionUid={topo})."
                )
            elif progress:
                progress(f"{site.upper()}: using tbv3 vm-status for power checks.")
        pending = []
        pending_vms: list[dict[str, Any]] = []
        for vm in selected:
            power = vm.get("powerState") or ""
            ok = is_powered_on(power) if want_on else is_powered_off(power)
            if not ok:
                pending.append(f"{vm['name']} ({power or 'unknown'})")
                pending_vms.append(vm)
        if on_wait:
            on_wait(selected, pending_vms)
        if not pending:
            if progress:
                progress(f"{site.upper()}: selected VMs are {label}.")
            return {"ok": True, "message": f"VMs {label}.", "vms": selected}
        if progress:
            progress(f"{site.upper()}: waiting for {label}: {', '.join(pending)}")
        time.sleep(poll_seconds)
    return {
        "ok": False,
        "message": f"Timed out waiting for VMs to be {label}.",
        "vms": last_current,
    }


def _save_payload(name: str, description: str) -> dict[str, Any]:
    # tbv3 requires description length 1–255; empty/null is rejected.
    desc = "" if description is None else str(description).strip()
    if not desc:
        desc = DEFAULT_SAVE_DESCRIPTION
    name_text = (name or "").strip()
    return {
        "saveDocuments": False,
        "name": name_text[:255],
        "description": desc[:255],
    }


def _save_looks_disabled(message: str) -> bool:
    text = (message or "").lower()
    return any(
        phrase in text
        for phrase in (
            "save disabled",
            "savedisabled",
            "save is disabled",
            "not allowed to save",
            "save not enabled",
            "saveenabled",
            "cannot save",
            "can't save",
        )
    )


def set_demo_save_enabled(token: str, site: str, demo_id: str, enabled: bool) -> dict[str, Any]:
    """Same REST call as the bot /esave and /dsave (not the old ajax admin UI)."""
    did = str(demo_id or "").strip()
    if not did:
        return {"ok": False, "message": "Content ID is required to toggle save."}
    url = f"{site_base(site)}/api/admin/demos/{did}"
    body = {"saveDisabled": "false" if enabled else "true"}
    try:
        response = _request("PUT", url, token, json_body=body)
    except requests.RequestException as exc:
        return {"ok": False, "message": str(exc)}
    parsed = _json_or_text(response)
    message = api_message(parsed) or f"HTTP {response.status_code}"
    ok = response.status_code < 400
    if isinstance(parsed, dict) and "success" in parsed:
        ok = parsed.get("success") is True
    return {"ok": ok, "message": message or ("Save enabled." if enabled else "Save disabled.")}


def _save_browser_headers(href: str) -> dict[str, str]:
    if "ciscodcloud.com" in href:
        origin = TBV3_UI
        referer = f"{TBV3_UI}/"
    else:
        parsed = urlparse(href)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        referer = f"{origin}/dashboard/sessions"
    return {
        "Accept": "application/json, text/plain, */*",
        "Origin": origin,
        "Referer": referer,
        "User-Agent": _BROWSER_UA,
    }


def _save_candidates(
    site: str,
    session_id: str,
    session: dict[str, Any] | None,
    payload: dict[str, Any],
) -> list[tuple[str, str, dict[str, Any] | None]]:
    """One save call: tbv3 POST when the session is v3 (savev3.har), else v2 PUT (savev2ui.har)."""
    topo = extract_topology_uid(session)
    if topo:
        v3_body = dict(payload)
        v3_body["sessionId"] = str(session_id)
        v3_body["topologyVersion"] = {"uid": topo}
        return [("POST", f"{TBV3_API}/api/session-save-actions", v3_body)]
    return [("PUT", f"{site_base(site)}/api/sessions/{session_id}/save", payload)]


def _first_id(values: list[Any]) -> str:
    for value in values:
        if value is None or value is False:
            continue
        text = str(value).strip()
        if text and text.lower() not in {"none", "null"}:
            return text
    return ""


def extract_save_ids(body: Any) -> dict[str, str]:
    found: dict[str, str] = {}

    def walk(obj: Any, depth: int = 0) -> None:
        if depth > 5 or obj is None:
            return
        if isinstance(obj, list):
            for item in obj[:20]:
                walk(item, depth + 1)
            return
        if not isinstance(obj, dict):
            return
        mapping = {
            "activeId": ("savedId",),
            "activeDemoId": ("savedId",),
            "savedId": ("savedId",),
            "demoId": ("demoId",),
            "contentId": ("contentId",),
            "parentId": ("parentId",),
            "topologyVersionUid": ("topologyVersionUid",),
            "name": ("savedName",),
        }
        for key, dests in mapping.items():
            raw = obj.get(key)
            if raw is None or isinstance(raw, (dict, list)):
                continue
            text = str(raw).strip()
            if not text or text.lower() in {"none", "null"}:
                continue
            for dest in dests:
                found.setdefault(dest, text)
        for nested in ("demo", "content", "save", "session", "data", "result"):
            walk(obj.get(nested), depth + 1)

    walk(body)
    return found


def list_saved_contents(token: str, site: str, *, state: str | None = "saved") -> list[dict[str, Any]]:
    url = f"{site_base(site)}/api/contents?expand=sharedWith"
    if state:
        url = f"{site_base(site)}/api/contents?state={state}&expand=sharedWith"
    try:
        response = _request("GET", url, token, timeout=30)
    except requests.RequestException as exc:
        return [], str(exc)
    if response.status_code == 401:
        return [], (
            "dCloud token was rejected (401). Use Sign in to dCloud at the top of the page "
            "to log in or import from browser, then try again."
        )
    if response.status_code >= 400:
        return [], api_message(_json_or_text(response)) or f"HTTP {response.status_code}"
    body = _json_or_text(response)
    if not isinstance(body, dict):
        return [], "Unexpected contents response."
    items = body.get("content") or body.get("contents") or []
    return [item for item in items if isinstance(item, dict)], None


def _content_states(item: dict[str, Any]) -> list[str]:
    raw = item.get("state") or item.get("states") or []
    if isinstance(raw, list):
        parts = [str(part).strip() for part in raw if str(part).strip()]
    else:
        text = str(raw or "").strip()
        parts = [text] if text else []
    # dCloud repeats a state on some records ("saved, promoted, shared, promoted").
    return unique_states(parts)


def unique_states(parts: list[str]) -> list[str]:
    """Drop repeated state words, keeping dCloud's order and spelling."""
    seen: set[str] = set()
    unique: list[str] = []
    for part in parts:
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(part)
    return unique


def _content_is_promoted(item: dict[str, Any] | None) -> bool:
    if not isinstance(item, dict):
        return False
    return any(part.lower() == "promoted" for part in _content_states(item))


def _content_saved_at(item: dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return ""
    dates = item.get("dates") if isinstance(item.get("dates"), dict) else {}
    for raw in (
        dates.get("saved"),
        item.get("updated"),
        dates.get("published"),
        item.get("published"),
        item.get("created"),
    ):
        text = str(raw or "").strip()
        if text:
            return text
    return ""


def summarize_saved_content(item: dict[str, Any], site: str) -> dict[str, Any]:
    uid = extract_demo_numeric_id(item)
    states = _content_states(item)
    promoted = _content_is_promoted(item)
    parent = extract_parent_content_id(item, saved_id=uid)
    topology_uid = extract_content_topology_uid(item, site)
    is_tbv3 = bool(topology_uid)
    return {
        "site": (site or "").strip().lower(),
        "contentId": uid,
        "name": str(item.get("name") or "").strip(),
        "owner": _owner_name(item),
        "state": ", ".join(states),
        "states": states,
        "promoted": promoted,
        "isTbv3": is_tbv3,
        "topologyUid": topology_uid,
        "deletable": not promoted or is_tbv3,
        "eolOnly": promoted and not is_tbv3,
        "local": bool(item.get("local")),
        "parentId": parent,
        "savedAt": _content_saved_at(item),
        "contentViewUrl": tbv3_edit_url(topology_uid) or (edit_topology_url(site, uid, item) if uid else ""),
    }


def list_saved_contents_for_site(token: str, site: str) -> tuple[list[dict[str, Any]], str | None]:
    site_code = (site or "").strip().lower()
    if site_code not in KNOWN_SITES:
        return [], "Datacenter must be SJC, RTP, LON, SNG, or SYD."
    items, err = list_saved_contents(token, site_code, state="saved")
    if err:
        return [], err
    rows = [summarize_saved_content(item, site_code) for item in items if extract_demo_numeric_id(item)]
    rows.sort(key=lambda row: (row.get("name") or "").lower())
    return rows, None


def list_saved_contents_all_sites(token: str) -> dict[str, Any]:
    contents: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(SITES)) as pool:
        futures = {
            pool.submit(list_saved_contents_for_site, token, site): site for site in SITES
        }
        for future in as_completed(futures):
            site = futures[future]
            rows, err = future.result()
            if err:
                errors[site] = err
            contents.extend(rows)
    order = {site: index for index, site in enumerate(SITES)}
    contents.sort(
        key=lambda row: (
            order.get(row.get("site") or "", 99),
            row.get("name") or "",
            row.get("contentId") or "",
        )
    )
    return {"contents": contents, "errors": errors}


def delete_saved_content(token: str, site: str, content_id: str) -> dict[str, Any]:
    """DELETE /api/contents/{id} — same as the dCloud custom-content dashboard delete."""
    site_code = (site or "").strip().lower()
    cid = str(content_id or "").strip()
    if site_code not in KNOWN_SITES:
        return {"ok": False, "site": site_code, "contentId": cid, "message": "Invalid datacenter."}
    if not cid:
        return {"ok": False, "site": site_code, "contentId": cid, "message": "Content ID is required."}
    details = fetch_content(token, site_code, cid)
    topology_uid = extract_content_topology_uid(details or {}, site_code)
    if _content_is_promoted(details) and not topology_uid:
        return {
            "ok": False,
            "site": site_code,
            "contentId": cid,
            "message": "This promoted content uses Topology Builder v2 — use the EOL process to delete it.",
            "locked": True,
            "builderVersion": "v2",
        }
    url = f"{site_base(site_code)}/api/contents/{cid}"
    try:
        response = _request("DELETE", url, token, timeout=60)
    except requests.RequestException as exc:
        return {"ok": False, "site": site_code, "contentId": cid, "message": str(exc)}
    body = _json_or_text(response)
    message = api_message(body) or f"HTTP {response.status_code}"
    ok = response.status_code < 400
    if isinstance(body, dict) and "success" in body:
        ok = body.get("success") is True
    if ok and not message:
        message = "Deleted."
    return {
        "ok": ok,
        "site": site_code,
        "contentId": cid,
        "message": message if message else ("Deleted." if ok else "Delete failed."),
    }


def list_pending_surveys(token: str, site: str) -> tuple[list[dict[str, Any]], str | None]:
    """GET /api/surveys — pending session feedback surveys (same as dashboard decline)."""
    site_code = (site or "").strip().lower()
    if site_code not in KNOWN_SITES:
        return [], "Datacenter must be SJC, RTP, LON, SNG, or SYD."
    url = f"{site_base(site_code)}/api/surveys"
    try:
        response = _request("GET", url, token)
    except requests.RequestException as exc:
        return [], str(exc)
    if response.status_code == 401:
        return [], (
            "dCloud token was rejected (401). Use Sign in to dCloud at the top of the page "
            "to log in or import from browser, then try again."
        )
    if response.status_code >= 400:
        return [], api_message(_json_or_text(response)) or f"HTTP {response.status_code}"
    body = _json_or_text(response)
    if not isinstance(body, dict):
        return [], "Unexpected surveys response."
    rows = body.get("surveys") or []
    if not isinstance(rows, list):
        return [], "Unexpected surveys response."
    out: list[dict[str, Any]] = []
    for survey in rows:
        if not isinstance(survey, dict):
            continue
        uid = str(survey.get("uid") or "").strip()
        if not uid:
            continue
        out.append(
            {
                "site": site_code,
                "surveyId": uid,
                "sessionId": str(survey.get("sessionId") or "").strip(),
                "name": str(survey.get("name") or "").strip(),
                "start": str(survey.get("start") or "").strip(),
                "stop": str(survey.get("stop") or "").strip(),
            }
        )
    return out, None


def list_pending_surveys_all_sites(token: str) -> dict[str, Any]:
    surveys: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(SITES)) as pool:
        futures = {
            pool.submit(list_pending_surveys, token, site): site for site in SITES
        }
        for future in as_completed(futures):
            site = futures[future]
            rows, err = future.result()
            if err:
                errors[site] = err
            surveys.extend(rows)
    order = {site: index for index, site in enumerate(SITES)}
    surveys.sort(
        key=lambda row: (
            order.get(row.get("site") or "", 99),
            row.get("name") or "",
            row.get("surveyId") or "",
        )
    )
    return {"surveys": surveys, "errors": errors}


def decline_survey(token: str, site: str, survey_id: str) -> dict[str, Any]:
    """PUT /api/surveys/{uid}/decline — dismiss session feedback survey (HAR: clear surveys.har)."""
    site_code = (site or "").strip().lower()
    uid = str(survey_id or "").strip()
    if site_code not in KNOWN_SITES:
        return {"ok": False, "site": site_code, "surveyId": uid, "message": "Invalid datacenter."}
    if not uid:
        return {"ok": False, "site": site_code, "surveyId": uid, "message": "Survey ID is required."}
    url = f"{site_base(site_code)}/api/surveys/{uid}/decline"
    try:
        response = _request("PUT", url, token, json_body={})
    except requests.RequestException as exc:
        return {"ok": False, "site": site_code, "surveyId": uid, "message": str(exc)}
    body = _json_or_text(response)
    message = api_message(body) or f"HTTP {response.status_code}"
    ok = response.status_code < 400
    if isinstance(body, dict) and "success" in body:
        ok = body.get("success") is True
    return {
        "ok": ok,
        "site": site_code,
        "surveyId": uid,
        "message": message if message else ("Declined." if ok else "Decline failed."),
    }


def decline_surveys(token: str, items: list[tuple[str, str]]) -> dict[str, Any]:
    if not items:
        return {"ok": True, "declined": 0, "failed": 0, "results": []}
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(len(items), 10)) as pool:
        futures = [
            pool.submit(decline_survey, token, site, survey_id)
            for site, survey_id in items
        ]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"ok": False, "message": str(exc)})
    declined = sum(1 for item in results if item.get("ok"))
    return {
        "ok": all(item.get("ok") for item in results),
        "declined": declined,
        "failed": len(results) - declined,
        "results": results,
    }


def fetch_content(token: str, site: str, content_id: str) -> dict[str, Any] | None:
    cid = str(content_id or "").strip()
    if not cid:
        return None
    url = f"{site_base(site)}/api/contents/{cid}"
    try:
        response = _request("GET", url, token, timeout=30)
    except requests.RequestException:
        return None
    if response.status_code >= 400:
        return None
    body = _json_or_text(response)
    return body if isinstance(body, dict) else None


def wait_until_saved(
    site: str,
    session_id: str,
    *,
    timeout_seconds: int = 900,
    poll_seconds: int = 10,
    progress: Progress | None = None,
) -> str:
    deadline = time.time() + timeout_seconds
    last = ""
    while time.time() < deadline:
        status, err = check_public_session_status(site, session_id)
        last = status or (err or "")
        key = _status_key(status)
        if key in {"saved", "12"} or (key.isdigit() and int(key) == 12):
            if progress:
                progress(f"{site.upper()}: save finished ({status}).")
            return status
        if key in {"cancelled", "canceled", "deleted", "failed", "error"}:
            return status
        if progress and last:
            progress(f"{site.upper()}: waiting for save to finish (currently {last})…")
        time.sleep(poll_seconds)
    return last


def _newest_content_by_name(
    token: str,
    site: str,
    name: str,
    skip: set[str],
) -> dict[str, Any] | None:
    wanted = (name or "").strip().lower()
    if not wanted:
        return None
    matches: list[dict[str, Any]] = []
    seen: set[str] = set()
    for state in ("saved", None):
        items, _err = list_saved_contents(token, site, state=state)
        for item in items:
            uid = str(item.get("uid") or item.get("id") or "").strip()
            if not uid or uid in skip or uid in seen:
                continue
            if str(item.get("name") or "").strip().lower() != wanted:
                continue
            seen.add(uid)
            matches.append(item)
    if not matches:
        return None

    def sort_key(item: dict[str, Any]) -> int:
        uid = str(item.get("uid") or "").strip()
        return int(uid) if uid.isdigit() else 0

    matches.sort(key=sort_key, reverse=True)
    return matches[0]


def collect_save_ids(
    token: str,
    site: str,
    session_id: str,
    save_body: Any,
    session: dict[str, Any] | None,
    progress: Progress | None = None,
    source_demo_id: str = "",
    save_name: str = "",
) -> dict[str, str]:
    """Saved content ID is session.activeId / activeDemoId — it already exists during the live session."""
    parent = str(
        (session or {}).get("parentId")
        or (session or {}).get("parentDemoId")
        or source_demo_id
        or ""
    ).strip()
    skip = {item for item in (str(session_id), str(source_demo_id or ""), parent) if item}

    from_save = extract_save_ids(save_body)
    ids = {
        key: value
        for key, value in from_save.items()
        if key != "savedId" or value not in skip
    }
    if save_name:
        ids["savedName"] = save_name
    ids["sessionId"] = str(session_id)

    wait_until_saved(site, session_id, timeout_seconds=900, progress=progress)
    time.sleep(2)
    details, _err = fetch_session(token, site, session_id, expand="all")
    if details:
        after = extract_save_ids(details)
        for key in ("parentId", "topologyVersionUid", "savedName"):
            if after.get(key) and not ids.get(key):
                ids[key] = after[key]
        parent = str(details.get("parentId") or details.get("parentDemoId") or parent).strip()
        if parent:
            skip.add(parent)
            ids.setdefault("parentId", parent)

    saved_id = session_saved_content_id(details) or session_saved_content_id(session)
    if saved_id in skip:
        saved_id = ""
    source = "session activeId/activeDemoId" if saved_id else ""
    if not saved_id and ids.get("savedId") and ids["savedId"] not in skip:
        saved_id, source = ids["savedId"], "save API"

    wanted_name = (save_name or ids.get("savedName") or "").strip()
    deadline = time.time() + 120
    while True:
        if saved_id:
            content = fetch_content(token, site, saved_id)
            if content:
                ids.setdefault("savedName", str(content.get("name") or ""))
                break
            if progress:
                progress(
                    f"{site.upper()}: waiting for content {saved_id} to appear in custom content…"
                )
        else:
            match = _newest_content_by_name(token, site, wanted_name, skip)
            if match:
                saved_id = str(match.get("uid") or "")
                source = "custom content list by save name"
                ids.setdefault("savedName", str(match.get("name") or ""))
                extra_match = extract_save_ids(match)
                for key in ("parentId", "topologyVersionUid"):
                    if extra_match.get(key):
                        ids.setdefault(key, extra_match[key])
                break
        if time.time() >= deadline:
            if saved_id and not fetch_content(token, site, saved_id):
                if progress:
                    progress(
                        f"{site.upper()}: {saved_id} did not show in GET /api/contents — "
                        "keeping the session activeId anyway."
                    )
            break
        time.sleep(10)

    if saved_id and saved_id not in skip:
        ids["savedId"] = saved_id
        ids["contentViewUrl"] = edit_topology_url(site, saved_id)
        if progress:
            progress(f"{site.upper()}: saved content ID {saved_id} ({source})")
    elif progress:
        progress(f"{site.upper()}: save finished but the custom-content ID was not found.")
    return ids


def save_session(
    token: str,
    site: str,
    session_id: str,
    *,
    save_url: str = "",
    save_method: str = "PUT",
    source_demo_id: str = "",
    name: str = "",
    description: str = "",
    progress: Progress | None = None,
) -> dict[str, Any]:
    details, err = fetch_session(token, site, session_id, expand="all")
    session = details if not err else None
    save_name = (name or "").strip() or str((session or {}).get("name") or "").strip()
    payload = _save_payload(save_name, description)
    if progress and save_name:
        progress(f"{site.upper()}: saving as {save_name!r} (no enable-save step first).")

    if save_url:
        method = (save_method or "PUT").upper()
        href = save_url.replace("{id}", session_id).replace("{sessionId}", session_id)
        if href.startswith("/"):
            href = site_base(site) + href
        candidates = [(method, href, payload)]
    else:
        candidates = _save_candidates(site, session_id, session, payload)

    def try_one(method: str, href: str, body: dict[str, Any] | None) -> tuple[requests.Response | None, str]:
        extra = _save_browser_headers(href)
        if progress:
            progress(f"{site.upper()}: trying save {method} {href}")
        try:
            response = _request(
                method,
                href,
                token,
                json_body=body,
                extra_headers=extra,
            )
        except requests.RequestException as exc:
            return None, str(exc)
        parsed = _json_or_text(response)
        detail = _short_http_message(parsed, response.status_code)
        if progress:
            progress(f"{site.upper()}: {method} {href} -> {response.status_code} {detail}")
        return response, detail

    def try_candidates() -> dict[str, Any]:
        last = "Save API not found."
        for method, href, body in candidates:
            response, detail = try_one(method, href, body)
            last = detail
            if response is None:
                continue
            parsed = _json_or_text(response)
            ok = response.status_code < 400
            if isinstance(parsed, dict) and "success" in parsed:
                ok = parsed.get("success") is True
            if not ok:
                continue
            ids = collect_save_ids(
                token,
                site,
                session_id,
                parsed,
                session,
                progress,
                source_demo_id,
                save_name=save_name,
            )
            return {
                "ok": True,
                "message": detail or "Session saved.",
                "statusCode": response.status_code,
                "url": href,
                **ids,
            }
        return {"ok": False, "message": last}

    result = try_candidates()
    if result.get("ok"):
        return result

    if source_demo_id and _save_looks_disabled(str(result.get("message") or "")):
        if progress:
            progress(
                f"{site.upper()}: save looks blocked. Trying bot /esave on content "
                f"{source_demo_id} with this token (not the old admin UI)…"
            )
        enabled = set_demo_save_enabled(token, site, source_demo_id, True)
        if progress:
            progress(f"{site.upper()}: enable-save: {enabled.get('message')}")
        if enabled.get("ok"):
            result = try_candidates()
            if result.get("ok") and progress:
                progress(
                    f"{site.upper()}: save worked after enabling. Disable save on "
                    f"{source_demo_id} later if you still want that content locked."
                )
            return result
        if progress:
            progress(
                f"{site.upper()}: this token may not work on the old admin enable-save UI. "
                "A dedicated HAR for that click would be needed if save stays blocked."
            )
    return result


def token_identities(token: str) -> set[str]:
    """CEC IDs / emails in the dCloud access token, used to tell my sessions from shared ones."""
    clean = normalize_dcloud_token(token)
    parts = clean.split(".")
    if len(parts) < 2:
        return set()
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError, binascii.Error):
        return set()
    if not isinstance(claims, dict):
        return set()
    names: set[str] = set()
    for key in ("ccoid", "sub", "email_address", "email", "preferred_username", "uid"):
        value = str(claims.get(key) or "").strip().lower()
        if not value:
            continue
        names.add(value)
        if "@" in value:
            names.add(value.split("@", 1)[0])
    return {name for name in names if name}


def session_owner(details: dict[str, Any] | None) -> str:
    """Owner CEC ID on a dCloud session or saved content record."""
    if not isinstance(details, dict):
        return ""
    return _owner_name(details)


def session_virtual_center(details: dict[str, Any] | None) -> str:
    """Virtual Center number dCloud shows next to the session ID.

    The list API uses virtualCenter; session details and tbv3 use
    virtualCenterId. expand=server may omit it, so callers must not treat an
    empty result as “clear the number we already have.”
    """
    if not isinstance(details, dict):
        return ""
    expand = details.get("expand") if isinstance(details.get("expand"), dict) else {}
    nested: list[dict[str, Any]] = []
    for key in ("session", "sessionDetails", "server"):
        value = details.get(key)
        if isinstance(value, dict):
            nested.append(value)
    server = expand.get("server")
    if isinstance(server, dict):
        nested.append(server)
    for obj in (details, expand, *nested):
        for key in ("virtualCenter", "virtualCenterId", "vc"):
            text = str(obj.get(key) or "").strip()
            if text and text.lower() not in {"none", "null"}:
                return text
    return ""


def owner_is_me(owner: str, identities: set[str] | None) -> bool | None:
    """True/False when both sides are known, None when we cannot tell."""
    name = str(owner or "").strip().lower()
    if not name or not identities:
        return None
    if name in identities:
        return True
    if "@" in name and name.split("@", 1)[0] in identities:
        return True
    return False


def shared_with_from_details(details: dict[str, Any] | None) -> list[dict[str, str]]:
    if not isinstance(details, dict):
        return []
    expand = details.get("expand") if isinstance(details.get("expand"), dict) else {}
    raw = expand.get("sharedWith")
    if not isinstance(raw, list):
        raw = details.get("sharedWith")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        user_id = str(item.get("userId") or "").strip()
        if not user_id:
            continue
        out.append(
            {
                "userId": user_id,
                "fullName": str(item.get("fullName") or "").strip(),
            }
        )
    return out


def search_share_users(
    token: str,
    site: str,
    query: str,
    *,
    content_scope: bool = False,
) -> tuple[list[dict[str, str]], str | None]:
    site_code = (site or "").strip().lower()
    name = str(query or "").strip()
    if site_code not in KNOWN_SITES:
        return [], "Datacenter must be SJC, RTP, LON, SNG, or SYD."
    if len(name) < 2:
        return [], None
    params = {"name": name}
    if content_scope:
        params["scope"] = "dsx"
    url = f"{site_base(site_code)}/api/users/search?{urlencode(params)}"
    try:
        response = _request("GET", url, token)
    except requests.RequestException as exc:
        return [], str(exc)
    if response.status_code == 401:
        return [], (
            "dCloud token was rejected (401). Use Sign in to dCloud at the top of the page "
            "to log in or import from browser, then try again."
        )
    if response.status_code >= 400:
        return [], api_message(_json_or_text(response)) or f"HTTP {response.status_code}"
    body = _json_or_text(response)
    if not isinstance(body, dict):
        return [], "Unexpected user search response."
    rows = body.get("users") or []
    if not isinstance(rows, list):
        return [], None
    out: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        user_id = str(row.get("userId") or "").strip()
        if not user_id:
            continue
        out.append(
            {
                "userId": user_id,
                "fullName": str(row.get("fullName") or "").strip(),
                "email": str(row.get("email") or "").strip(),
            }
        )
    return out, None


def fetch_session_shared_with(
    token: str,
    site: str,
    session_id: str,
) -> tuple[list[dict[str, str]], str | None]:
    details, err = fetch_session(token, site, session_id, expand="sharedWith")
    if err:
        return [], err
    return shared_with_from_details(details), None


def fetch_content_shared_with(
    token: str,
    site: str,
    content_id: str,
) -> tuple[list[dict[str, str]], str | None]:
    site_code = (site or "").strip().lower()
    cid = str(content_id or "").strip()
    if site_code not in KNOWN_SITES:
        return [], "Datacenter must be SJC, RTP, LON, SNG, or SYD."
    if not cid:
        return [], "Content ID is required."
    url = f"{site_base(site_code)}/api/contents/{cid}?expand=sharedWith"
    try:
        response = _request("GET", url, token, timeout=30)
    except requests.RequestException as exc:
        return [], str(exc)
    if response.status_code == 401:
        return [], (
            "dCloud token was rejected (401). Use Sign in to dCloud at the top of the page "
            "to log in or import from browser, then try again."
        )
    if response.status_code >= 400:
        return [], api_message(_json_or_text(response)) or f"HTTP {response.status_code}"
    body = _json_or_text(response)
    if not isinstance(body, dict):
        return [], "Unexpected content response."
    return shared_with_from_details(body), None


def _share_payload(shared_with: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in shared_with or []:
        if isinstance(item, dict):
            user_id = str(item.get("userId") or "").strip()
        else:
            user_id = str(item or "").strip()
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        rows.append({"userId": user_id})
    return {"sharedWith": rows}


def update_session_share(
    token: str,
    site: str,
    session_id: str,
    shared_with: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    site_code = (site or "").strip().lower()
    sid = str(session_id or "").strip()
    if site_code not in KNOWN_SITES:
        return False, "Datacenter must be SJC, RTP, LON, SNG, or SYD."
    if not sid:
        return False, "Session ID is required."
    url = f"{site_base(site_code)}/api/sessions/{sid}/share"
    payload = _share_payload(shared_with)
    try:
        response = _request("PUT", url, token, json_body=payload)
    except requests.RequestException as exc:
        return False, str(exc)
    if response.status_code == 401:
        return False, (
            "dCloud token was rejected (401). Use Sign in to dCloud at the top of the page "
            "to log in or import from browser, then try again."
        )
    if response.status_code >= 400:
        return False, api_message(_json_or_text(response)) or f"HTTP {response.status_code}"
    return True, None


def update_content_share(
    token: str,
    site: str,
    content_id: str,
    shared_with: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    site_code = (site or "").strip().lower()
    cid = str(content_id or "").strip()
    if site_code not in KNOWN_SITES:
        return False, "Datacenter must be SJC, RTP, LON, SNG, or SYD."
    if not cid:
        return False, "Content ID is required."
    url = f"{site_base(site_code)}/api/contents/{cid}/share"
    payload = _share_payload(shared_with)
    try:
        response = _request("PUT", url, token, json_body=payload, timeout=30)
    except requests.RequestException as exc:
        return False, str(exc)
    if response.status_code == 401:
        return False, (
            "dCloud token was rejected (401). Use Sign in to dCloud at the top of the page "
            "to log in or import from browser, then try again."
        )
    if response.status_code >= 400:
        return False, api_message(_json_or_text(response)) or f"HTTP {response.status_code}"
    return True, None
