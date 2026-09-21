"""Content Automation Hub (dcloud-cai) VM replacement — HTML forms, Cisco SSO cookies."""

from __future__ import annotations

import re
import time
from html.parser import HTMLParser
from typing import Any

import requests

from net_errors import describe_request_error
import urllib3

from browser_auth.chrome_profiles import chrome_cookie_files, chrome_cookie_snapshot
from dcloud_client import TBV3_UI

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CAI_BASE = "https://dcloud-cai.cisco.com"
CAI_HOME = CAI_BASE + "/"
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

# Session-manager DCs → CAI path segments (dropdown uses EMEA/APJ, not LON/SNG).
CAI_DC_FOR_SITE = {
    "sjc": ("sjc",),
    "rtp": ("rtp",),
    "lon": ("emea", "lon"),
    "sng": ("apj", "sng"),
    "syd": ("syd",),
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def cai_demo_url(site: str, saved_id: str) -> str:
    dc = (CAI_DC_FOR_SITE.get((site or "").strip().lower()) or (site or "sjc",))[0]
    return f"{CAI_BASE}/demo/{dc}/{str(saved_id).strip()}/"


def _strip(html: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html or "")).strip()


class _FormVmParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.vms: list[str] = []
        self.integrate_dcs: list[str] = []
        self._capture_title = False
        self._title_buf = ""
        self._in_replace = False
        self._in_integrate = False
        self.tasks: list[dict[str, str]] = []
        self._in_task_table = False
        self._headers: list[str] = []
        self._row: list[str] = []
        self._cell = False
        self._cell_buf = ""
        self._is_th = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "title":
            self._capture_title = True
            self._title_buf = ""
        if tag == "form" and "replace" in (a.get("action") or "") and a.get("id") == "replaceRequest":
            self._in_replace = True
        if tag == "form" and a.get("id") == "integrate":
            self._in_integrate = True
        if tag == "input" and self._in_replace and a.get("type") == "checkbox" and a.get("name"):
            name = a["name"].strip()
            if name and name not in self.vms:
                self.vms.append(name)
        if tag == "input" and self._in_integrate and a.get("type") == "checkbox" and a.get("name"):
            name = a["name"].strip().lower()
            if name and name not in self.integrate_dcs:
                self.integrate_dcs.append(name)
        if tag == "table":
            self._in_task_table = True
            self._headers = []
        if self._in_task_table and tag in {"td", "th"}:
            self._cell = True
            self._is_th = tag == "th"
            self._cell_buf = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._capture_title = False
            self.title = _WS_RE.sub(" ", self._title_buf).strip()
        if tag == "form":
            self._in_replace = False
            self._in_integrate = False
        if self._in_task_table and tag in {"td", "th"} and self._cell:
            text = _WS_RE.sub(" ", self._cell_buf).strip()
            if self._is_th:
                self._headers.append(text)
            else:
                self._row.append(text)
            self._cell = False
        if self._in_task_table and tag == "tr" and self._row:
            self._flush_row()
        if tag == "table":
            if self._row:
                self._flush_row()
            self._in_task_table = False
            self._headers = []

    def handle_data(self, data: str) -> None:
        if self._capture_title:
            self._title_buf += data
        if self._cell:
            self._cell_buf += data

    def _flush_row(self) -> None:
        cells = self._row
        self._row = []
        headers = [h.lower() for h in self._headers]
        if not cells:
            return
        # Demo-page mini table: Type, Server, Status, New ID
        if any("server" == h for h in headers) and "demo" not in headers:
            row = {
                "type": cells[0] if cells else "",
                "server": cells[1] if len(cells) > 1 else "",
                "status": cells[2] if len(cells) > 2 else "",
                "newId": cells[3] if len(cells) > 3 else "",
                "demo": "",
                "dc": "",
            }
            if "replacement" in row["type"].lower() or "integrat" in row["type"].lower() or row["server"]:
                self.tasks.append(row)
            return
        # Homepage: Owner, Type, Demo, Status, DC, Server, Updated, New ID
        def col(*names: str) -> str:
            for name in names:
                if name in headers:
                    idx = headers.index(name)
                    if idx < len(cells):
                        return cells[idx]
            return ""

        if "demo" in headers or "status" in headers:
            row = {
                "owner": col("owner"),
                "type": col("type"),
                "demo": col("demo"),
                "status": col("status"),
                "dc": col("dc"),
                "server": col("server"),
                "updated": col("updated"),
                "newId": col("new id", "newid"),
            }
            if row["demo"] or row["status"]:
                self.tasks.append(row)


def _session(cookie_header: str) -> requests.Session:
    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": _BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": CAI_HOME,
        }
    )
    header = (cookie_header or "").strip()
    if header:
        sess.headers["Cookie"] = header
    return sess


_SSO_HOST_HINTS = ("duosecurity", "id.cisco.com", "cloudsso", "login.cisco.com", "sso.cisco.com")


def _looks_signed_out(html: str, status: int, final_url: str) -> bool:
    """Only an SSO bounce or a login form means signed out.

    A demo page can load fine and still have no replace/integrate form, which is a
    wrong Target ID rather than an auth problem.
    """
    host = (final_url or "").lower()
    if any(hint in host for hint in _SSO_HOST_HINTS):
        return True
    if status in {401, 403}:
        return True
    text = (html or "").lower()
    if "dcloud-cai" in host:
        return False
    return "single sign-on" in text or 'name="pf.username"' in text


def _looks_logged_in(html: str, status: int, final_url: str) -> bool:
    if status >= 400:
        return False
    host = (final_url or "").lower()
    if "duosecurity" in host or "login" in host and "dcloud-cai" not in host:
        return False
    text = html or ""
    if "Tasks:" in text and ("Replacement" in text or "Select DC or Template" in text):
        return True
    if 'id="replaceRequest"' in text or "Comfirm VM Replacement" in text:
        return True
    if 'id="integrate"' in text or "[Integration]" in text:
        return True
    if "Request Submitted" in text:
        return True
    return False


def probe_cai_login(cookie_header: str) -> dict[str, Any]:
    sess = _session(cookie_header)
    try:
        resp = sess.get(CAI_HOME, timeout=30, allow_redirects=True)
    except requests.RequestException as exc:
        return {"ok": False, "loggedIn": False, "message": describe_request_error(exc, "CAI")}
    html = resp.text or ""
    logged = _looks_logged_in(html, resp.status_code, str(resp.url))
    if logged:
        return {"ok": True, "loggedIn": True, "message": "CAI session is active."}
    return {
        "ok": False,
        "loggedIn": False,
        "message": (
            "Not signed in to Content Automation Hub. Click Connect to CAI — a tool browser "
            "window opens for Cisco SSO/Duo. After that the tool refreshes CAI itself."
        ),
    }


def normalize_cookie_header(raw: str) -> str:
    text = (raw or "").strip()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    parts = [part.strip() for part in text.replace("\n", ";").split(";") if part.strip() and "=" in part]
    return "; ".join(parts)


_TRACKING_COOKIE_NAMES = frozenset({
    "s_ecid",
    "optanonconsent",
    "optanonalertboxclosed",
    "_abck",
    "bm_sz",
    "dcloud_rdp",
    "utag_main",
    "unicaniodid",
    "s_cc",
    "s_sq",
})


def cookie_header_is_tracking_only(header: str) -> bool:
    names: list[str] = []
    for part in (header or "").split(";"):
        if "=" not in part:
            continue
        name = part.split("=", 1)[0].strip().lower()
        if name:
            names.append(name)
    if not names:
        return True
    return all(
        name in _TRACKING_COOKIE_NAMES or name.startswith("amcv_") or name.startswith("kndctr_")
        for name in names
    )


def _cai_cookies_from_jar(jar) -> dict[str, str]:
    now = time.time()
    cookies: dict[str, str] = {}
    for cookie in jar:
        domain = (cookie.domain or "").lstrip(".").lower()
        if "dcloud-cai.cisco.com" not in domain:
            continue
        if cookie.expires and cookie.expires < now:
            continue
        if cookie.name and cookie.value:
            cookies[cookie.name] = cookie.value
    return cookies


def import_cai_cookies_from_chrome() -> tuple[str | None, str]:
    try:
        import browser_cookie3
    except ImportError:
        return None, "Install browser-cookie3 to import the CAI session from Chrome."

    files = chrome_cookie_files()
    if not files:
        return None, "Chrome cookie database not found."

    best_header = ""
    best_count = 0
    notes: list[str] = []
    for cookie_file in files:
        try:
            with chrome_cookie_snapshot(cookie_file) as snapshot:
                jar = browser_cookie3.chrome(
                    cookie_file=str(snapshot),
                    domain_name="dcloud-cai.cisco.com",
                )
                cookies = _cai_cookies_from_jar(jar)
        except Exception as exc:
            err = str(exc).strip().split("\n")[0]
            notes.append(f"{cookie_file.parent.name}: {err[:120]}")
            continue
        if not cookies:
            continue
        header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if len(cookies) > best_count:
            best_count = len(cookies)
            best_header = header

    if best_header:
        return best_header, f"Imported {best_count} CAI cookie(s) from Chrome."
    extra = notes[0] if notes else "Chrome did not save a dcloud-cai.cisco.com login cookie"
    return (
        None,
        f"{extra}. CAI keeps the session in the live tab, not in Chrome's cookie file. "
        "On the CAI tab: DevTools → Network → click https://dcloud-cai.cisco.com/ → "
        "Request Headers → Cookie → copy the whole value → paste it below. "
        "The Application tab .cisco.com cookies are not the CAI login.",
    )


def capture_cai_session(*, headed: bool | None = None, timeout_s: float = 180) -> tuple[str | None, str]:
    """Sign in to CAI in the tool Chromium profile and return a Cookie header."""
    from tool_browser import capture_site_cookies, profile_exists

    hosts = ("dcloud-cai.cisco.com",)

    def logged_in(cookie: str) -> bool:
        return bool(probe_cai_login(cookie).get("loggedIn"))

    silent = headed is False or (headed is None and profile_exists())
    if silent:
        header, message = capture_site_cookies(
            CAI_HOME,
            hosts,
            logged_in,
            headed=False,
            timeout_s=min(25.0, timeout_s),
        )
        if header:
            return header, message
        if headed is False:
            return None, message
    return capture_site_cookies(
        CAI_HOME,
        hosts,
        logged_in,
        headed=True,
        timeout_s=timeout_s,
    )


def _dc_candidates(site: str) -> list[str]:
    site_code = (site or "").strip().lower()
    ordered = list(CAI_DC_FOR_SITE.get(site_code) or (site_code,))
    if site_code and site_code not in ordered:
        ordered.append(site_code)
    return [dc for dc in ordered if dc]


def _get_html(cookie_header: str, url: str) -> tuple[requests.Response | None, str]:
    sess = _session(cookie_header)
    try:
        resp = sess.get(url, timeout=30, allow_redirects=True)
    except requests.RequestException as exc:
        return None, describe_request_error(exc, "CAI")
    return resp, ""


def fetch_demo_page(cookie_header: str, site: str, saved_id: str) -> dict[str, Any]:
    saved = str(saved_id or "").strip()
    if not saved:
        return {"ok": False, "message": "Saved content ID is required."}
    last_err = "CAI demo page not found."
    for dc in _dc_candidates(site):
        url = f"{CAI_BASE}/demo/{dc}/{saved}/"
        resp, err = _get_html(cookie_header, url)
        if err:
            last_err = err
            continue
        if resp is None:
            continue
        if _looks_signed_out(resp.text or "", resp.status_code, str(resp.url)):
            return {
                "ok": False,
                "loggedIn": False,
                "message": (
                    "CAI is not reachable from this machine. Join the Cisco network and click Connect to CAI."
                ),
            }
        if resp.status_code == 404:
            last_err = f"{dc.upper()}: demo {saved} not found in CAI."
            continue
        if resp.status_code >= 400:
            last_err = f"{dc.upper()}: HTTP {resp.status_code}"
            continue
        parser = _FormVmParser()
        parser.feed(resp.text or "")
        html = resp.text or ""
        has_replace = bool(parser.vms) or "replaceRequest" in html
        has_integrate = bool(parser.integrate_dcs) or 'id="integrate"' in html
        if not has_replace and not has_integrate:
            last_err = (
                f"{dc.upper()}: CAI has no replace/integrate form for {saved}. "
                "Check the Target ID — it must be the saved content ID, not the parent demo."
            )
            continue
        return {
            "ok": True,
            "loggedIn": True,
            "caiDc": dc,
            "caiUrl": url,
            "title": parser.title,
            "vms": parser.vms,
            "integrateDcs": parser.integrate_dcs,
            "tasks": parser.tasks,
        }
    return {"ok": False, "message": last_err}


def submit_vm_replace(
    cookie_header: str,
    site: str,
    saved_id: str,
    *,
    vm_names: list[str],
    target_demoid: str,
    cai_dc: str = "",
) -> dict[str, Any]:
    saved = str(saved_id or "").strip()
    target = str(target_demoid or "").strip()
    vms = [str(name).strip() for name in vm_names if str(name).strip()]
    if not saved:
        return {"ok": False, "message": "Saved content ID is required."}
    if not target:
        return {"ok": False, "message": "Target (promoted / published) demo ID is required."}
    if not vms:
        return {"ok": False, "message": "Select at least one VM to replace."}
    dc = (cai_dc or "").strip().lower() or _dc_candidates(site)[0]
    url = f"{CAI_BASE}/replace/{dc}/{saved}"
    data: list[tuple[str, str]] = [(name, "on") for name in vms]
    data.append(("target_demoid", target))
    sess = _session(cookie_header)
    sess.headers["Referer"] = f"{CAI_BASE}/demo/{dc}/{saved}/"
    sess.headers["Origin"] = CAI_BASE
    sess.headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        resp = sess.post(url, data=data, timeout=60, allow_redirects=True)
    except requests.RequestException as exc:
        return {"ok": False, "message": describe_request_error(exc, "CAI")}
    html = resp.text or ""
    if not _looks_logged_in(html, resp.status_code, str(resp.url)) and "Request Submitted" not in html:
        return {
            "ok": False,
            "loggedIn": False,
            "message": "CAI session expired during submit. Open CAI in Chrome and import again.",
        }
    if resp.status_code >= 400:
        return {"ok": False, "message": f"Replace failed (HTTP {resp.status_code})."}
    submitted = "Request Submitted" in html or resp.status_code in {200, 302}
    if not submitted:
        return {"ok": False, "message": "CAI did not confirm the replacement request."}
    return {
        "ok": True,
        "message": "Request submitted.",
        "caiDc": dc,
        "caiUrl": f"{CAI_BASE}/demo/{dc}/{saved}/",
    }


CAI_INTEGRATE_CODES = frozenset({"rtp", "sjc", "emea", "apj", "syd", "idev"})


def normalize_cai_dc(code: str) -> str:
    text = str(code or "").strip().lower()
    if ":" in text:
        text = text.split(":", 1)[0]
    aliases = {
        "lon": "emea",
        "emear": "emea",
        "sng": "apj",
        "infra": "idev",
        "infradev": "idev",
    }
    return aliases.get(text, text)


def cai_integrate_dests(raw_dcs: list[str] | tuple[str, ...] | None) -> list[str]:
    """Map CAMGR dest guids / CAI codes to Integrate checkboxes (skip ContentDEV)."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_dcs or []:
        dc = normalize_cai_dc(str(raw or ""))
        if not dc or dc.startswith("contentdev"):
            continue
        if dc not in CAI_INTEGRATE_CODES:
            continue
        if dc in seen:
            continue
        seen.add(dc)
        out.append(dc)
    return out


def cai_dc_label(code: str) -> str:
    dc = normalize_cai_dc(code)
    labels = {
        "rtp": "RTP",
        "sjc": "SJC",
        "emea": "EMEA",
        "apj": "APJ",
        "syd": "SYD",
        "idev": "InfraDEV",
    }
    return labels.get(dc, (dc or "").upper())


def cai_dc_to_dcloud_site(dc: str) -> str:
    """CAI dest checkbox → dCloud custom-content site (EMEA/APJ use lon/sng)."""
    mapping = {
        "rtp": "rtp",
        "sjc": "sjc",
        "emea": "lon",
        "apj": "sng",
        "syd": "syd",
    }
    return mapping.get(normalize_cai_dc(dc), "")


def parse_cai_new_id(raw: str, dest: str = "") -> str:
    """CAI Tasks New ID cell: `483747`, `[sjc] 483747`, or `[sjc] none`."""
    text = str(raw or "").strip()
    if not text:
        return ""
    wanted = normalize_cai_dc(dest)
    tagged = re.findall(r"\[([A-Za-z]+)\]\s*([A-Za-z0-9_-]+)", text)
    if tagged:
        by_dc = {normalize_cai_dc(dc): nid for dc, nid in tagged}
        if wanted and wanted in by_dc:
            text = by_dc[wanted]
        elif len(by_dc) == 1:
            text = next(iter(by_dc.values()))
        else:
            text = by_dc.get(wanted, "")
    low = str(text or "").strip().lower()
    if not low or low in {"none", "n/a", "-", "—"} or "none" in low:
        return ""
    nums = re.findall(r"\d{3,}", str(text))
    return nums[-1] if nums else ""


def submit_integrate(
    cookie_header: str,
    site: str,
    saved_id: str,
    *,
    dest_dcs: list[str],
    cai_dc: str = "",
    dest_fields: list[str] | None = None,
) -> dict[str, Any]:
    saved = str(saved_id or "").strip()
    dests = []
    seen: set[str] = set()
    for raw in dest_dcs:
        dc = normalize_cai_dc(str(raw or ""))
        if not dc or dc in seen:
            continue
        seen.add(dc)
        dests.append(dc)
    if not saved:
        return {"ok": False, "message": "Saved content ID is required."}
    if not dests:
        return {"ok": False, "message": "Select at least one destination DC to integrate."}
    dc = (cai_dc or "").strip().lower() or _dc_candidates(site)[0]
    url = f"{CAI_BASE}/integrate/{dc}/{saved}"
    # CAI's own checkbox names win: its EMEA box is "lon", which our canonical
    # "emea" code would never match, so that dest silently never submitted.
    fields: list[str] = []
    for raw in dest_fields or dests:
        name = str(raw or "").strip()
        if name and name not in fields:
            fields.append(name)
    data: list[tuple[str, str]] = [(name, "on") for name in fields]
    sess = _session(cookie_header)
    sess.headers["Referer"] = f"{CAI_BASE}/demo/{dc}/{saved}/"
    sess.headers["Origin"] = CAI_BASE
    sess.headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        resp = sess.post(url, data=data, timeout=60, allow_redirects=True)
    except requests.RequestException as exc:
        return {"ok": False, "message": describe_request_error(exc, "CAI")}
    html = resp.text or ""
    if not _looks_logged_in(html, resp.status_code, str(resp.url)) and "Request Submitted" not in html:
        return {
            "ok": False,
            "loggedIn": False,
            "message": "CAI session expired during submit. Open CAI in Chrome and import again.",
        }
    if resp.status_code >= 400:
        return {"ok": False, "message": f"Integrate failed (HTTP {resp.status_code})."}
    submitted = "Request Submitted" in html or resp.status_code in {200, 302}
    if not submitted:
        return {"ok": False, "message": "CAI did not confirm the integration request."}
    return {
        "ok": True,
        "message": "Request submitted.",
        "caiDc": dc,
        "caiUrl": f"{CAI_BASE}/demo/{dc}/{saved}/",
        "dcs": dests,
    }


CAI_TEMPLATE_CODES = frozenset({"rtp", "sjc", "lon", "apj", "syd"})
# Site prefix used in the folder name CAMGR emails after a transfer, e.g. RTP/1348363.
CAI_TEMPLATE_SITE_CODES = frozenset({"rtp", "sjc", "lon", "apj", "syd", "emea", "sng"})


def normalize_cai_template_path(raw: str) -> str:
    """Canonical CAI template source folder.

    CAI folder names are case sensitive: CDEV.RTP/vPod-23 works, cdev.rtp/vpod-23
    fails with Error. Two shapes are valid:
      CDEV.RTP/vPod-23  - a VM sitting in a Content Dev vPod (that DC only)
      RTP/1348363       - a demo that was transferred out, per the CAMGR email
    """
    text = re.sub(r"\s+", "", str(raw or "").strip()).strip("/")
    if not text:
        return ""
    dev = re.fullmatch(r"cdev[.\-]([a-z0-9_-]+)/vpod-(\d+)", text, re.IGNORECASE)
    if dev:
        return f"CDEV.{dev.group(1).upper()}/vPod-{dev.group(2)}"
    demo = re.fullmatch(r"([a-z]{3})/(\d+)", text, re.IGNORECASE)
    if demo and demo.group(1).lower() in CAI_TEMPLATE_SITE_CODES:
        return f"{demo.group(1).upper()}/{demo.group(2)}"
    return ""


def cai_template_path_kind(path: str) -> str:
    """"dev" for a Content Dev vPod folder, "demo" for a transferred demo, else ""."""
    canonical = normalize_cai_template_path(path)
    if not canonical:
        return ""
    return "dev" if canonical.upper().startswith("CDEV.") else "demo"


def cai_template_path_home_dc(path: str) -> str:
    """The only DC a Content Dev vPod can be templated in."""
    canonical = normalize_cai_template_path(path)
    if not canonical.upper().startswith("CDEV."):
        return ""
    return canonical.split("/", 1)[0].split(".", 1)[1].lower()


def submit_template(
    cookie_header: str,
    source_path: str,
    *,
    server: str,
    dest_dcs: list[str],
) -> dict[str, Any]:
    """Create CAI templates from one VM in a Content Dev vPod or a transferred demo."""
    path = normalize_cai_template_path(source_path)
    vm_name = str(server or "").strip()
    dests: list[str] = []
    seen: set[str] = set()
    for raw in dest_dcs:
        dc = str(raw or "").strip().lower()
        aliases = {"emea": "lon", "sng": "apj"}
        dc = aliases.get(dc, dc)
        if dc in CAI_TEMPLATE_CODES and dc not in seen:
            seen.add(dc)
            dests.append(dc)
    if not path:
        return {
            "ok": False,
            "message": (
                "Enter a source folder like CDEV.RTP/vPod-23 (Content Dev) or RTP/1348363 "
                "(a demo you transferred). CAI folder names are case sensitive."
            ),
        }
    if not vm_name:
        return {"ok": False, "message": "Enter the exact VM name in that folder."}
    if not dests:
        return {"ok": False, "message": "Select at least one template destination."}
    home = cai_template_path_home_dc(path)
    if home and [dc for dc in dests if dc != home]:
        # The VM only exists in the dev vPod's own DC, so other sites always Error.
        return {
            "ok": False,
            "message": (
                f"{path} only exists in {home.upper()}, so the template can only be made there. "
                "To reach the other DCs, put the VM in a blank demo, save it, transfer that demo "
                "out, then use the folder from the transfer email (like RTP/1348363)."
            ),
        }
    url = f"{CAI_BASE}/template/{path}/"
    data: list[tuple[str, str]] = [("server", vm_name)]
    data.extend((dc, "on") for dc in dests)
    sess = _session(cookie_header)
    sess.headers["Referer"] = url
    sess.headers["Origin"] = CAI_BASE
    sess.headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        resp = sess.post(url, data=data, timeout=60, allow_redirects=True)
    except requests.RequestException as exc:
        return {"ok": False, "message": describe_request_error(exc, "CAI")}
    html = resp.text or ""
    if not _looks_logged_in(html, resp.status_code, str(resp.url)) and "Request Submitted" not in html:
        return {
            "ok": False,
            "loggedIn": False,
            "message": "CAI session expired during submit. Open CAI in Chrome and import again.",
        }
    if resp.status_code >= 400:
        return {"ok": False, "message": f"Template request failed (HTTP {resp.status_code})."}
    if "Request Submitted" not in html and resp.status_code not in {200, 302}:
        return {"ok": False, "message": "CAI did not confirm the template request."}
    return {
        "ok": True,
        "message": "Template request submitted.",
        "sourcePath": path,
        "server": vm_name,
        "dcs": dests,
        "caiUrl": url,
    }


def parse_cai_task_dc(raw: str) -> tuple[str, str]:
    """Split CAI Tasks DC column (`rtp => sjc`) into source, dest."""
    text = str(raw or "").strip().lower().replace("→", "=>")
    if "=>" in text:
        left, right = text.split("=>", 1)
        return normalize_cai_dc(left), normalize_cai_dc(right)
    dc = normalize_cai_dc(text)
    return "", dc


def match_template_tasks(
    tasks: list[dict[str, str]],
    *,
    vm_name: str,
    dests: list[str] | None = None,
) -> list[dict[str, str]]:
    """CAI Tasks rows for a template request. Templates have no demo ID, so match on VM name."""
    vm = str(vm_name or "").strip().lower()
    wanted = {normalize_cai_dc(dc) for dc in (dests or []) if normalize_cai_dc(dc)}
    hits: list[dict[str, str]] = []
    for task in tasks:
        if "template" not in str(task.get("type") or "").lower():
            continue
        server = str(task.get("server") or "").lower()
        if vm and vm not in server:
            continue
        _source, dest = parse_cai_task_dc(str(task.get("dc") or ""))
        if wanted and dest and dest not in wanted:
            continue
        hits.append(task)
    return hits


def match_integrate_task(
    tasks: list[dict[str, str]],
    *,
    saved_id: str,
    dests: list[str] | None = None,
) -> list[dict[str, str]]:
    saved = str(saved_id).strip()
    wanted = {normalize_cai_dc(dc) for dc in (dests or []) if normalize_cai_dc(dc)}
    hits: list[dict[str, str]] = []
    for task in tasks:
        if "integration" not in str(task.get("type") or "").lower():
            continue
        demo = str(task.get("demo") or "").strip()
        if demo and demo != saved:
            continue
        _source, dest = parse_cai_task_dc(str(task.get("dc") or ""))
        if wanted and dest and dest not in wanted:
            continue
        hits.append(task)
    return hits


def list_cai_tasks(cookie_header: str) -> dict[str, Any]:
    resp, err = _get_html(cookie_header, CAI_HOME)
    if err:
        return {"ok": False, "message": err, "tasks": []}
    if resp is None:
        return {"ok": False, "message": "No response from CAI.", "tasks": []}
    if not _looks_logged_in(resp.text or "", resp.status_code, str(resp.url)):
        return {
            "ok": False,
            "loggedIn": False,
            "message": "CAI session expired. Open CAI in Chrome and import again.",
            "tasks": [],
        }
    parser = _FormVmParser()
    parser.feed(resp.text or "")
    return {"ok": True, "loggedIn": True, "tasks": parser.tasks}


def normalize_task_status(raw: str) -> str:
    text = (raw or "").strip().lower()
    if "complete" in text:
        return "completed"
    if "error" in text or "fail" in text:
        return "error"
    if "queue" in text:
        return "queuing"
    if "process" in text:
        return "processing"
    # Empty stays empty so callers can fall back to the request's own status
    # instead of showing a dest as "Unknown" before CAI lists a task for it.
    return text


_INTEGRATE_STATUS_LABELS = {
    "completed": "Completed",
    "processing": "Processing",
    "queuing": "Queued",
    "submitted": "Submitted",
    "waiting": "Waiting",
    "error": "Error",
}


def _prefer_integrate_task(tasks: list[dict[str, str]]) -> dict[str, str] | None:
    order = ("processing", "queuing", "submitted", "error", "completed")
    ranked = [(normalize_task_status(str(task.get("status") or "")), task) for task in tasks]
    for wanted in order:
        for status, task in ranked:
            if status == wanted:
                return task
    return tasks[0] if tasks else None


def cai_integrate_dc_chips(
    dests: list[str] | None,
    tasks: list[dict[str, str]] | None,
    *,
    overall: str = "",
    previous: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Per-dest CAI Integrate pills, same idea as CAI Tasks (`rtp => sjc` vs `rtp => rtp`)."""
    names: list[str] = []
    seen: set[str] = set()
    for raw in dests or []:
        dc = normalize_cai_dc(str(raw or ""))
        if not dc or dc in seen:
            continue
        seen.add(dc)
        names.append(dc)
    grouped: dict[str, list[dict[str, str]]] = {}
    for task in tasks or []:
        _source, dest = parse_cai_task_dc(str(task.get("dc") or ""))
        if dest:
            grouped.setdefault(dest, []).append(task)
            if dest not in seen:
                seen.add(dest)
                names.append(dest)
    prev_by_dc: dict[str, dict[str, Any]] = {}
    for chip in previous or []:
        if not isinstance(chip, dict):
            continue
        dc = normalize_cai_dc(str(chip.get("dc") or ""))
        if dc:
            prev_by_dc[dc] = chip
    chips: list[dict[str, Any]] = []
    fallback = normalize_task_status(overall) if overall else "queuing"
    for dest in names:
        task = _prefer_integrate_task(grouped.get(dest) or [])
        source, _dest_from_task = parse_cai_task_dc(str((task or {}).get("dc") or ""))
        status = normalize_task_status(str((task or {}).get("status") or "")) or fallback
        if not task and fallback == "completed":
            status = "completed"
        pretty = _INTEGRATE_STATUS_LABELS.get(status, status.replace("_", " ").title() or "Queued")
        path = f"{source} => {dest}" if source else dest
        done = status == "completed"
        new_id = parse_cai_new_id(str((task or {}).get("newId") or ""), dest)
        old = prev_by_dc.get(dest) or {}
        if not new_id:
            new_id = str(old.get("newId") or "").strip()
        topology_uid = str(old.get("topologyUid") or "").strip()
        href = str(old.get("href") or "").strip()
        if topology_uid and not topology_uid.isdigit():
            href = f"{TBV3_UI}/edit/{topology_uid}"
        elif href.startswith(TBV3_UI) and "/edit/" in href:
            pass
        else:
            href = ""
        href_checked = True if href else bool(old.get("hrefChecked"))
        label_dc = cai_dc_label(dest)
        if new_id:
            label = f"{label_dc} {new_id}"
        elif done:
            label = f"{label_dc} ✓"
        else:
            label = f"{label_dc} {pretty}"
        tip = f"{path} · {pretty}"
        if new_id:
            tip += f" · custom content {new_id}"
        if href:
            tip += " · Topology Builder v3"
        chips.append(
            {
                "dc": dest,
                "source": source,
                "phase": status,
                "status": status,
                "done": done,
                "newId": new_id,
                "topologyUid": topology_uid,
                "href": href,
                "hrefChecked": href_checked,
                "label": label,
                "tip": tip,
            }
        )
    return chips


def parse_cai_task_server(raw: str) -> tuple[str, str]:
    """CAI Tasks Server cell: `480730 || UCCX150SUB` → ("480730", "UCCX150SUB")."""
    text = str(raw or "").strip()
    if "||" in text:
        demo, vm = text.split("||", 1)
        return demo.strip(), vm.strip()
    return "", text


def match_replace_task(
    tasks: list[dict[str, str]],
    *,
    saved_id: str,
    vm_name: str,
    target_id: str = "",
) -> dict[str, str] | None:
    saved = str(saved_id).strip()
    vm = str(vm_name).strip().lower()
    target = str(target_id).strip()
    # An exact VM-name match must win: a substring match would let UCCX150
    # pick up the UCCX150SUB row and report one VM's status for both.
    exact: list[dict[str, str]] = []
    loose: list[dict[str, str]] = []
    for task in tasks:
        if "replacement" not in str(task.get("type") or "").lower():
            continue
        demo = str(task.get("demo") or "").strip()
        server = str(task.get("server") or "")
        if demo and demo != saved:
            # Demo-page tasks often have empty demo; homepage has it.
            continue
        if target and target not in server and demo != saved:
            continue
        if not vm:
            exact.append(task)
            continue
        _server_demo, server_vm = parse_cai_task_server(server)
        if server_vm.strip().lower() == vm:
            exact.append(task)
        elif vm in server.lower():
            loose.append(task)
    hits = exact or loose
    if not hits:
        for task in tasks:
            if "replacement" not in str(task.get("type") or "").lower():
                continue
            server = str(task.get("server") or "")
            if target and target not in server:
                continue
            _server_demo, server_vm = parse_cai_task_server(server)
            if vm and server_vm.strip().lower() == vm:
                exact.append(task)
            elif vm and vm in server.lower():
                loose.append(task)
        hits = exact or loose
    if not hits:
        return None
    # Prefer the newest-looking row (homepage lists newest first).
    return hits[0]


_REPLACE_STATUS_LABELS = {
    "completed": "Completed",
    "processing": "Processing",
    "queuing": "Queued",
    "submitted": "Submitted",
    "error": "Error",
}


def cai_replace_vm_chips(
    vms: list[str] | tuple[str, ...] | None,
    tasks: list[dict[str, str]] | None,
    *,
    saved_id: str,
    target_id: str = "",
    overall: str = "",
    previous: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Per-VM pills for a replacement: CAI queues one task per VM, each with its own status."""
    names: list[str] = []
    seen: set[str] = set()
    for raw in vms or []:
        name = str(raw or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        names.append(name)
    prev_by_vm: dict[str, dict[str, Any]] = {}
    for chip in previous or []:
        if not isinstance(chip, dict):
            continue
        key = str(chip.get("vm") or "").strip().lower()
        if key:
            prev_by_vm[key] = chip
    fallback = normalize_task_status(overall) if overall else ""
    chips: list[dict[str, Any]] = []
    for name in names:
        old = prev_by_vm.get(name.lower()) or {}
        task = match_replace_task(
            tasks or [],
            saved_id=saved_id,
            vm_name=name,
            target_id=target_id,
        )
        status = normalize_task_status(str((task or {}).get("status") or ""))
        old_status = normalize_task_status(str(old.get("status") or old.get("phase") or ""))
        if not status:
            # A finished task drops off the CAI homepage, so a missing row must not
            # walk a VM backwards from Completed/Error to Queued.
            status = old_status if old_status in {"completed", "error"} else (old_status or fallback or "queuing")
        new_id = parse_cai_new_id(str((task or {}).get("newId") or ""))
        if not new_id:
            new_id = str(old.get("newId") or "").strip()
        source, dest = parse_cai_task_dc(str((task or {}).get("dc") or ""))
        server = str((task or {}).get("server") or "").strip() or str(old.get("server") or "").strip()
        updated = str((task or {}).get("updated") or "").strip() or str(old.get("updated") or "").strip()
        done = status == "completed"
        pretty = _REPLACE_STATUS_LABELS.get(status, status.replace("_", " ").title() or "Queued")
        label = f"{name} ✓" if done else f"{name} {pretty}"
        tip_parts = [f"{name} · {pretty}"]
        if source or dest:
            tip_parts.append(f"{source} => {dest}" if source else dest)
        if server:
            tip_parts.append(server)
        if new_id:
            tip_parts.append(f"new ID {new_id}")
        if updated:
            tip_parts.append(f"updated {updated}")
        chips.append(
            {
                "vm": name,
                "phase": status,
                "status": status,
                "done": done,
                "newId": new_id,
                "server": server,
                "updated": updated,
                "dc": dest,
                "source": source,
                "label": label,
                "tip": " · ".join(tip_parts),
            }
        )
    return chips
