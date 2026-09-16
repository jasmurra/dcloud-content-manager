"""Content Automation Manager (dcloud-camgr) transfer/integration JSON API."""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
import urllib3

from browser_auth.chrome_profiles import chrome_cookie_files, chrome_cookie_snapshot
from net_errors import describe_request_error
from camgr_tab import (
    chrome_tab_request,
    connect_camgr_via_chrome_tab,
    probe_camgr_via_chrome_tab,
    using_chrome_tab,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CAMGR_BASE = "https://dcloud-camgr.cisco.com"
CAMGR_HOME = CAMGR_BASE + "/#/cas"
CAMGR_API = CAMGR_BASE + "/ca/api"
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

# Session-manager DCs → CAMGR API path + server guid (LON→EMEAR, SNG→APJ).
CAMGR_DC_FOR_SITE = {
    "sjc": (("sjc", "SJC"),),
    "rtp": (("rtp", "RTP"),),
    "lon": (("emear", "EMEAR"), ("lon", "LON")),
    "sng": (("apj", "APJ"), ("sng", "SNG")),
    "syd": (("syd", "SYD"),),
}

IN_FLIGHT_STATUSES = frozenset(
    {"READY", "EXPORTING", "EXPORTED", "XFRING", "XFRED", "IMPORTING"}
)
TERMINAL_STATUSES = frozenset({"COMPLETE", "ERROR"})
_SESSION_COOKIE_HINTS = ("mod_auth_openidc", "openidc", "session")
# One per login attempt; leftovers from extra tabs, useless for staying signed in.
_STATE_COOKIE_PREFIX = "mod_auth_openidc_state"
CAMGR_LOGIN_HINT = "Click Connect to CAMGR while Content Transfer is open in Chrome."
_cookie_sink: Any = None


def set_camgr_cookie_sink(sink: Any) -> None:
    """Register a callback that receives a refreshed Cookie header.

    CAMGR rotates mod_auth_openidc_session as you use it. Without this the tool
    keeps replaying the copy it imported once and eventually gets logged out.
    """
    global _cookie_sink
    _cookie_sink = sink


def _is_state_cookie(name: str) -> bool:
    return str(name or "").lower().startswith(_STATE_COOKIE_PREFIX)


def parse_cookie_header(header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in str(header or "").split(";"):
        name, sep, value = part.partition("=")
        name = name.strip()
        if sep and name:
            cookies[name] = value.strip()
    return cookies


def build_cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(
        f"{name}={value}"
        for name, value in cookies.items()
        if name and value and not _is_state_cookie(name)
    )


def camgr_home_url() -> str:
    return CAMGR_HOME


def site_to_camgr_guid(site: str) -> str:
    site_code = (site or "").strip().lower()
    mapped = CAMGR_DC_FOR_SITE.get(site_code)
    if mapped:
        return mapped[0][1]
    return (site or "").strip().upper() or "RTP"


def camgr_guid_to_site(guid: str) -> str:
    code = str(guid or "").strip().upper()
    if ":" in code:
        code = code.split(":", 1)[0]
    if not code:
        return ""
    for site, pairs in CAMGR_DC_FOR_SITE.items():
        for path, mapped in pairs:
            if mapped == code or str(path).upper() == code:
                return site
    return code.lower()


def _path_candidates(site: str) -> list[tuple[str, str]]:
    site_code = (site or "").strip().lower()
    ordered = list(CAMGR_DC_FOR_SITE.get(site_code) or ((site_code, site_code.upper()),))
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for path, guid in ordered:
        path = str(path or "").strip().lower()
        guid = str(guid or "").strip().upper()
        if not path or path in seen:
            continue
        seen.add(path)
        out.append((path, guid or path.upper()))
    return out


def _session(cookie_header: str) -> requests.Session:
    if using_chrome_tab(cookie_header):
        raise RuntimeError("CAMGR Chrome tab session cannot be sent as an HTTP Cookie header.")
    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": _BROWSER_UA,
            "Accept": "application/json, text/plain, */*",
            "Origin": CAMGR_BASE,
            "Referer": CAMGR_BASE + "/",
        }
    )
    header = (cookie_header or "").strip()
    if header:
        sess.headers["Cookie"] = header
    sent = parse_cookie_header(header)

    def _remember_rotated(resp: requests.Response, *_args: Any, **_kwargs: Any) -> None:
        if not _cookie_sink or not getattr(resp, "cookies", None):
            return
        merged = dict(sent)
        changed = False
        for cookie in resp.cookies:
            name = str(cookie.name or "")
            value = str(cookie.value or "")
            if not name or not value or _is_state_cookie(name):
                continue
            if merged.get(name) != value:
                merged[name] = value
                changed = True
        if not changed:
            return
        try:
            _cookie_sink(build_cookie_header(merged))
        except Exception:
            pass

    sess.hooks["response"].append(_remember_rotated)
    return sess


def _looks_sso(resp: requests.Response) -> bool:
    url = str(resp.url or "").lower()
    if any(part in url for part in ("duosecurity", "cloudsso", "id.cisco.com", "login.cisco.com")):
        return True
    if "dcloud-camgr.cisco.com" not in url and "login" in url:
        return True
    ctype = str(resp.headers.get("Content-Type") or "").lower()
    text = (resp.text or "")[:800].lower()
    if "html" in ctype and ("duo" in text or "sign in" in text or "openidc" in text):
        return True
    return False


def _json_body(resp: requests.Response) -> Any:
    ctype = str(resp.headers.get("Content-Type") or "").lower()
    text = (resp.text or "").strip()
    if "json" in ctype or text.startswith("{") or text.startswith("["):
        try:
            return resp.json()
        except ValueError:
            return None
    return None


def _auth_error(message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "loggedIn": False,
        "message": message,
    }


def probe_camgr_login(cookie_header: str) -> dict[str, Any]:
    header = (cookie_header or "").strip()
    if using_chrome_tab(header):
        return probe_camgr_via_chrome_tab()
    if header:
        sess = _session(header)
        try:
            resp = sess.get(f"{CAMGR_API}/users/current", timeout=30, allow_redirects=True)
        except requests.RequestException as exc:
            return _auth_error(describe_request_error(exc, "CAMGR") or "Could not reach CAMGR.")
        if "dcloud-camgr.cisco.com" in str(resp.url or "").lower():
            body = _json_body(resp)
            user = ""
            if isinstance(body, dict):
                for key in ("id", "username", "user", "userId"):
                    user = str(body.get(key) or "").strip()
                    if user:
                        break
            if resp.status_code < 400 and user:
                return {
                    "ok": True,
                    "loggedIn": True,
                    "user": user,
                    "access": body.get("access") if isinstance(body, dict) and isinstance(body.get("access"), dict) else {},
                    "jobs": body.get("jobs") if isinstance(body, dict) else None,
                    "message": f"CAMGR session is active ({user}).",
                }
    tab = probe_camgr_via_chrome_tab()
    if tab.get("loggedIn"):
        return tab
    return _auth_error(tab.get("message") or CAMGR_LOGIN_HINT)


def _camgr_cookies_from_jar(jar) -> dict[str, str]:
    now = time.time()
    cookies: dict[str, str] = {}
    for cookie in jar:
        domain = (cookie.domain or "").lstrip(".").lower()
        if "dcloud-camgr.cisco.com" not in domain:
            continue
        if cookie.expires and cookie.expires < now:
            continue
        # Login-state cookies pile up with every extra CAMGR tab and never carry the session.
        if _is_state_cookie(cookie.name):
            continue
        if cookie.name and cookie.value:
            cookies[cookie.name] = cookie.value
    return cookies


def _cookie_score(cookies: dict[str, str]) -> int:
    score = len(cookies)
    for name in cookies:
        lower = name.lower()
        if any(hint in lower for hint in _SESSION_COOKIE_HINTS):
            score += 10
    return score


def _decrypt_cookie_variants(browser: Any, encrypted_value: bytes, plain: str) -> list[str]:
    """Chrome v24 cookies prepend a domain hash; older rows in the same DB do not."""
    values: list[str] = []
    seen: set[str] = set()
    if plain:
        seen.add(plain)
        values.append(plain)
    decrypt = getattr(browser, "_decrypt", None)
    if not decrypt or not encrypted_value:
        return values
    for strip_domain_hash in (True, False):
        try:
            decoded = decrypt(b"", encrypted_value, strip_domain_hash)
        except Exception:
            continue
        text = str(decoded or "").strip()
        if text and text not in seen:
            seen.add(text)
            values.append(text)
    return values


def _camgr_headers_from_snapshot(snapshot: Path) -> list[str]:
    try:
        import browser_cookie3
        import sqlite3
    except ImportError:
        return []
    try:
        browser = browser_cookie3.Chrome(
            cookie_file=str(snapshot),
            domain_name="dcloud-camgr.cisco.com",
        )
    except Exception:
        return []
    try:
        con = sqlite3.connect(str(snapshot))
        rows = con.execute(
            "select name, value, encrypted_value from cookies "
            "where host_key like '%dcloud-camgr.cisco.com%'"
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return []
    variants: list[dict[str, str]] = [{}]
    for name, value, encrypted in rows:
        cookie_name = str(name or "")
        if not cookie_name or _is_state_cookie(cookie_name):
            continue
        enc = bytes(encrypted) if encrypted else b""
        options = _decrypt_cookie_variants(browser, enc, str(value or "").strip())
        if not options:
            continue
        next_variants: list[dict[str, str]] = []
        for base in variants:
            for option in options:
                merged = dict(base)
                merged[cookie_name] = option
                next_variants.append(merged)
        variants = next_variants[:4]
    headers: list[str] = []
    seen: set[str] = set()
    for cookies in variants:
        header = build_cookie_header(cookies)
        if header and header not in seen:
            seen.add(header)
            headers.append(header)
    return headers


def import_camgr_cookies_from_chrome() -> tuple[str | None, str]:
    files = chrome_cookie_files()
    notes: list[str] = []
    if not files:
        return None, "Chrome cookie database not found."
    for cookie_file in files:
        profile = cookie_file.parent.parent.name if cookie_file.parent.name == "Network" else cookie_file.parent.name
        try:
            with chrome_cookie_snapshot(cookie_file) as snapshot:
                headers = _camgr_headers_from_snapshot(snapshot)
                if not headers:
                    # Fall back to browser_cookie3's single decrypt.
                    try:
                        import browser_cookie3
                        jar = browser_cookie3.chrome(
                            cookie_file=str(snapshot),
                            domain_name="dcloud-camgr.cisco.com",
                        )
                        cookies = _camgr_cookies_from_jar(jar)
                        header = build_cookie_header(cookies)
                        if header:
                            headers = [header]
                    except Exception:
                        headers = []
        except Exception as exc:
            notes.append(f"{profile}: {str(exc).strip().split(chr(10))[0][:120]}")
            continue
        for header in headers:
            if probe_camgr_login(header).get("loggedIn"):
                return header, f"Imported CAMGR session from Chrome ({profile})."
    extra = notes[0] if notes else "Could not read CAMGR session from Chrome"
    return None, f"{extra}. Open CAMGR, then Connect."


def _get_json(cookie_header: str, url: str) -> tuple[Any, requests.Response | None, str]:
    if using_chrome_tab(cookie_header):
        status, body, err = chrome_tab_request("GET", url)
        if err:
            return None, None, err
        if status in {401, 403} or status == 0:
            return None, None, "CAMGR session expired."
        if status >= 400:
            return None, None, f"CAMGR HTTP {status}"
        return body, None, ""
    sess = _session(cookie_header)
    try:
        resp = sess.get(url, timeout=45, allow_redirects=True)
    except requests.RequestException as exc:
        return None, None, describe_request_error(exc, "CAMGR")
    if _looks_sso(resp) or resp.status_code in {401, 403}:
        return None, resp, "CAMGR session expired."
    body = _json_body(resp)
    if resp.status_code >= 400:
        return None, resp, f"CAMGR HTTP {resp.status_code}"
    return body, resp, ""


def is_cdev_camgr_guid(guid: str) -> bool:
    """ContentDEV transfers into a vPod, so its ":N" suffix is a vPod, not the integrate flag."""
    code = str(guid or "").strip().upper()
    if ":" in code:
        code = code.split(":", 1)[0]
    return code == "CDEV" or code.startswith("CDEV.")


def camgr_cdev_home_dc(guid: str) -> str:
    """ContentDEV.RTP only pairs with RTP — dev content moves to and from its own DC."""
    code = str(guid or "").strip().upper()
    if ":" in code:
        code = code.split(":", 1)[0]
    if not code.startswith("CDEV."):
        return ""
    return code.split(".", 1)[1].strip()


def _vpod_number(name: str) -> str:
    """CAMGR labels a vPod folder "23 :: vPod-23-jasmurra" and transfers to "CDEV.RTP:23"."""
    match = re.search(r"vpod[\s_-]*(\d+)", str(name or ""), re.IGNORECASE)
    if not match:
        match = re.search(r"(\d+)", str(name or ""))
    return match.group(1) if match else ""


def _vpod_vm(item: dict[str, Any]) -> dict[str, Any]:
    """One VM inside a vPod. CAMGR's vim ids look like "vm-17847266"; jobs want the number."""
    ident = str(item.get("parentServerId") or item.get("id") or "").strip()
    digits = re.sub(r"\D", "", ident)
    name = str(item.get("name") or "").strip() or ident
    return {
        "name": name,
        "id": ident,
        "parentServerId": int(digits) if digits else None,
        "os": str(item.get("description") or item.get("status") or "").strip(),
    }


def list_camgr_vpods(cookie_header: str, guid: str) -> list[dict[str, Any]]:
    """vPod folders a ContentDEV/InfraDEV server offers, with the VMs sitting in each one."""
    path = str(guid or "").strip().lower()
    if not path:
        return []
    body, _, err = _get_json(cookie_header, f"{CAMGR_BASE}/{path}/ca/api/vim/vpods")
    if err or not isinstance(body, list):
        return []
    # VMs arrive either nested under a folder's "devs" or as siblings pointing back via "parent".
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for item in body:
        if not isinstance(item, dict) or item.get("folder"):
            continue
        parent = str(item.get("parent") or "").strip()
        if parent:
            by_parent.setdefault(parent, []).append(item)
    vpods: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in body:
        if not isinstance(item, dict):
            continue
        if not item.get("folder") and str(item.get("parent") or "").strip():
            continue
        name = str(item.get("name") or "").strip()
        folder_id = str(item.get("id") or "").strip()
        number = _vpod_number(name)
        key = (folder_id, number)
        if key in seen:
            continue
        seen.add(key)
        raw_vms = [dev for dev in (item.get("devs") or []) if isinstance(dev, dict)]
        raw_vms.extend(by_parent.get(folder_id, []))
        vms: list[dict[str, Any]] = []
        vm_seen: set[str] = set()
        for raw in raw_vms:
            vm = _vpod_vm(raw)
            if not vm["id"] or vm["id"] in vm_seen:
                continue
            vm_seen.add(vm["id"])
            vms.append(vm)
        if number and name:
            label = f"{number} :: {name}"
        else:
            label = name or number or folder_id
        vpods.append(
            {
                "value": number,
                "name": name or (f"vPod-{number}" if number else folder_id),
                "id": folder_id,
                "label": label,
                "vms": vms,
            }
        )
    vpods.sort(key=lambda row: (int(row["value"]) if row["value"].isdigit() else 10**9, row["name"]))
    return vpods


def fetch_camgr_vpod_vms(cookie_header: str, guid: str, vpod: str) -> dict[str, Any]:
    """VMs sitting in one ContentDEV vPod — the dev-side stand-in for a demo's VM list."""
    server = str(guid or "").strip()
    wanted = str(vpod or "").strip()
    if not server or not wanted:
        return {"ok": False, "loggedIn": True, "message": "Pick a ContentDEV datacenter and a vPod."}
    vpods = list_camgr_vpods(cookie_header, server)
    if not vpods:
        return {
            "ok": False,
            "loggedIn": True,
            "message": f"No vPods came back from {server}. Check that CAMGR is still signed in.",
        }
    match = next((row for row in vpods if str(row.get("value")) == wanted), None)
    if match is None:
        match = next((row for row in vpods if str(row.get("id")) == wanted), None)
    if match is None:
        return {"ok": False, "loggedIn": True, "message": f"vPod {wanted} is not on {server} any more."}
    vms = match.get("vms") or []
    return {
        "ok": True,
        "loggedIn": True,
        "camgrDc": server,
        "vpod": str(match.get("value") or wanted),
        "label": str(match.get("label") or ""),
        "vms": vms,
        "vpods": vpods,
        "message": f"{len(vms)} VM(s) in {match.get('label') or wanted}.",
    }


def list_camgr_servers(cookie_header: str) -> dict[str, Any]:
    body, _, err = _get_json(cookie_header, f"{CAMGR_API}/servers")
    if err == "CAMGR session expired.":
        return _auth_error(err)
    if err:
        return {"ok": False, "loggedIn": True, "message": err, "servers": []}
    servers = []
    if isinstance(body, list):
        for item in body:
            if not isinstance(item, dict):
                continue
            guid = str(item.get("guid") or item.get("dc") or "").strip()
            dc = str(item.get("dc") or guid).strip()
            if not guid:
                continue
            is_cdev = bool(item.get("isCDEV") or item.get("cdev"))
            is_idev = bool(item.get("idev"))
            vpods = list_camgr_vpods(cookie_header, guid) if (is_cdev or is_idev) else []
            servers.append(
                {
                    "dc": dc,
                    "guid": guid,
                    "label": dc,
                    "usable": item.get("usable"),
                    "isCDEV": is_cdev,
                    "isIDEV": is_idev,
                    "homeDc": camgr_cdev_home_dc(guid),
                    "isOnline": item.get("isOnline") is not False,
                    "vpods": vpods,
                    "requiresVpod": bool(vpods),
                }
            )
    return {"ok": True, "loggedIn": True, "servers": servers}


def fetch_camgr_demo(cookie_header: str, site: str, saved_id: str) -> dict[str, Any]:
    saved = str(saved_id or "").strip()
    if not saved:
        return {"ok": False, "loggedIn": True, "message": "Saved content ID is required."}
    last_err = "Could not load this demo in CAMGR."
    for path, guid in _path_candidates(site):
        body, _, err = _get_json(cookie_header, f"{CAMGR_BASE}/{path}/ca/api/demos/{saved}")
        if err == "CAMGR session expired.":
            return _auth_error(err)
        if err or not isinstance(body, dict):
            last_err = err or last_err
            continue
        demo_id = str(body.get("pkdemoId") or saved).strip()
        root_id = str(body.get("fkrootDemoId") or body.get("fkRootDemoId") or "").strip()
        # A demo that points at itself is the original base, which is different
        # from CAMGR not knowing a root at all.
        root_is_self = bool(root_id) and (root_id == demo_id or root_id == saved)
        if root_is_self:
            root_id = ""
        return {
            "ok": True,
            "loggedIn": True,
            "site": (site or "").strip().lower(),
            "savedId": saved,
            "demoId": demo_id,
            "rootDemoId": root_id,
            "rootIsSelf": root_is_self,
            "name": str(body.get("dname") or "").strip(),
            "owner": str(body.get("fkownerId") or "").strip(),
            "camgrPath": path,
            "camgrDc": guid,
            "demo": {
                "name": str(body.get("dname") or "").strip(),
                "owner": str(body.get("fkownerId") or "").strip(),
                "saved": bool(body.get("saved")),
                "published": bool(body.get("published")),
            },
        }
    return {"ok": False, "loggedIn": True, "message": last_err}


def fetch_camgr_vms(cookie_header: str, site: str, saved_id: str) -> dict[str, Any]:
    demo = fetch_camgr_demo(cookie_header, site, saved_id)
    if not demo.get("ok"):
        return demo
    path = str(demo.get("camgrPath") or "")
    saved = str(demo.get("savedId") or saved_id).strip()
    body, _, err = _get_json(cookie_header, f"{CAMGR_BASE}/{path}/ca/api/demos/{saved}/vms")
    if err == "CAMGR session expired.":
        return _auth_error(err)
    if err:
        return {"ok": False, "loggedIn": True, "message": err}
    vms: list[dict[str, Any]] = []
    seen: set[int] = set()
    if isinstance(body, list):
        for item in body:
            if not isinstance(item, dict):
                continue
            parent = item.get("parentServerId")
            try:
                parent_id = int(parent)
            except (TypeError, ValueError):
                continue
            if parent_id in seen:
                continue
            seen.add(parent_id)
            vms.append(
                {
                    "name": str(item.get("server") or "").strip() or str(parent_id),
                    "parentServerId": parent_id,
                    "serverId": item.get("serverId"),
                    "os": str(item.get("os") or "").strip(),
                    "owner": str(item.get("owner") or "").strip(),
                }
            )
    demo["vms"] = vms
    demo["message"] = f"Loaded {len(vms)} CAMGR VM(s)."
    return demo


def _demo_id_int(saved_id: str) -> int | str:
    text = str(saved_id or "").strip()
    try:
        return int(text)
    except ValueError:
        return text


def _dest_tokens(dcs: list[str], *, integrate: bool, integrate_wait: bool) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    suffix = ""
    if integrate:
        suffix = ":2" if integrate_wait else ":1"
    for raw in dcs:
        guid = str(raw or "").strip()
        if not guid:
            continue
        if ":" in guid and guid.rsplit(":", 1)[-1].isdigit():
            # ContentDEV/InfraDEV carry a vPod number here, so leave the token alone.
            token = guid
        elif is_cdev_camgr_guid(guid):
            token = guid
        else:
            token = f"{guid}{suffix}"
        key = token.upper()
        if key in seen:
            continue
        seen.add(key)
        tokens.append(token)
    return tokens


def list_camgr_jobs(
    cookie_header: str,
    *,
    only_not_status: str = "COMPLETE",
    only_user: bool | None = True,
    only_dc: str = "",
    only_demo: str = "",
    only_status: str = "",
) -> dict[str, Any]:
    params: dict[str, str] = {}
    if only_not_status:
        params["onlyNotStatus"] = only_not_status
    if only_user is not None:
        params["onlyUser"] = "true" if only_user else "false"
    if only_dc:
        params["onlyDC"] = str(only_dc).strip()
    if only_demo:
        params["onlyDemo"] = str(only_demo).strip()
    if only_status:
        params["onlyStatus"] = only_status
    query = f"?{urlencode(params)}" if params else ""
    body, _, err = _get_json(cookie_header, f"{CAMGR_API}/jobs{query}")
    if err == "CAMGR session expired.":
        return _auth_error(err)
    if err:
        return {"ok": False, "loggedIn": True, "message": err, "jobs": []}
    jobs = [item for item in body if isinstance(item, dict)] if isinstance(body, list) else []
    return {"ok": True, "loggedIn": True, "jobs": jobs}


def _int_pct(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def public_camgr_job(item: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    status = str(item.get("status") or "").strip()
    progress = item.get("progress")
    try:
        progress_n = int(progress)
    except (TypeError, ValueError):
        progress_n = 0
    dc_status = []
    for row in item.get("dcStatus") or []:
        if not isinstance(row, dict):
            continue
        dc_status.append(
            {
                "dc": str(row.get("dc") or "").strip(),
                "importing": _int_pct(row.get("importing")),
                "xfring": _int_pct(row.get("xfring")),
                "integrating": _int_pct(row.get("integrating")),
            }
        )
    dests = []
    for raw in item.get("dcs") or []:
        text = str(raw or "").strip()
        if text:
            dests.append(text)
    return {
        "guid": str(item.get("guid") or "").strip(),
        "status": status.lower(),
        "statusRaw": status,
        "progress": progress_n,
        "sessionId": item.get("sessionId") or 0,
        "demoId": item.get("demoId"),
        "dc": str(item.get("dc") or "").strip(),
        "dcs": dests,
        "owner": str(item.get("owner") or "").strip(),
        "dcStatus": dc_status,
        "servers": _server_ids(item.get("servers") or []),
    }


def format_camgr_status(item: dict[str, Any] | None) -> str:
    pub = public_camgr_job(item) if item and item.get("statusRaw") else (item or {})
    status = str(pub.get("statusRaw") or pub.get("status") or "").strip()
    if not status:
        return ""
    progress = pub.get("progress")
    try:
        progress_n = int(progress)
    except (TypeError, ValueError):
        progress_n = None
    if progress_n is not None and status.upper() not in TERMINAL_STATUSES:
        return f"{status} {progress_n}%"
    return status


def _dc_name(raw: str) -> str:
    text = str(raw or "").strip()
    if ":" in text:
        text = text.rsplit(":", 1)[0]
    return text


def format_camgr_dc_chip(row: dict[str, Any] | None, *, overall: str = "") -> dict[str, Any]:
    """Per-dest CAMGR pill, same idea as Content Transfer (RTP ✓ vs SJC XFRING 0%)."""
    item = row if isinstance(row, dict) else {}
    dc = _dc_name(str(item.get("dc") or "")).upper() or "DC"
    importing = _int_pct(item.get("importing"))
    xfring = _int_pct(item.get("xfring"))
    job = str(overall or "").strip().upper()
    done = False
    if importing >= 100 or job == "COMPLETE":
        phase = "IMPORTED"
        pct = 100 if importing >= 100 or job == "COMPLETE" else importing
        done = True
    elif importing > 0:
        phase = "IMPORTING"
        pct = importing
    elif xfring >= 100:
        phase = "XFRED"
        pct = 100
    elif xfring > 0:
        phase = "XFRING"
        pct = xfring
    elif job in {"XFRING", "XFRED", "IMPORTING", "EXPORTED"}:
        phase = "XFRING"
        pct = 0
    elif job in {"EXPORTING", "READY"}:
        phase = "WAITING"
        pct = 0
    elif job == "ERROR":
        phase = "ERROR"
        pct = 0
    else:
        phase = job or "PENDING"
        pct = 0
    tip = f"{phase} {pct}%" if not done else "IMPORTED 100%"
    if done:
        label = f"{dc} ✓"
    elif phase == "WAITING":
        label = f"{dc} waiting"
    else:
        label = f"{dc} {phase} {pct}%"
    return {
        "dc": dc,
        "phase": phase.lower(),
        "percent": pct,
        "done": done,
        "label": label,
        "tip": tip,
    }


def camgr_dc_status_chips(
    dests: list[str] | None,
    dc_status: list[dict[str, Any]] | None,
    *,
    overall: str = "",
) -> list[dict[str, Any]]:
    by_dc: dict[str, dict[str, Any]] = {}
    for row in dc_status or []:
        if not isinstance(row, dict):
            continue
        name = _dc_name(str(row.get("dc") or "")).upper()
        if name:
            by_dc[name] = row
    names: list[str] = []
    seen: set[str] = set()
    for raw in dests or []:
        name = _dc_name(str(raw or "")).upper()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    if not names:
        names = list(by_dc.keys())
    return [
        format_camgr_dc_chip({**(by_dc.get(name) or {}), "dc": name}, overall=overall)
        for name in names
    ]


def _server_ids(values: list[Any]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for raw in values:
        try:
            num = int(raw)
        except (TypeError, ValueError):
            continue
        if num in seen:
            continue
        seen.add(num)
        out.append(num)
    return out


def _dcs_base(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = str(raw or "").strip()
        if ":" in text:
            text = text.rsplit(":", 1)[0]
        key = text.upper()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def match_camgr_job(
    jobs: list[dict[str, Any]],
    *,
    guid: str = "",
    demo_id: str = "",
    source_dc: str = "",
    servers: list[int] | None = None,
    dest_dcs: list[str] | None = None,
    owner: str = "",
) -> dict[str, Any] | None:
    wanted_guid = str(guid or "").strip()
    if wanted_guid:
        for item in jobs:
            if str(item.get("guid") or "").strip() == wanted_guid:
                return item
    demo = str(demo_id or "").strip()
    source = str(source_dc or "").strip().upper()
    wanted_servers = set(_server_ids(servers or []))
    wanted_dcs = {d.upper() for d in _dcs_base(dest_dcs or [])}
    owner_id = str(owner or "").strip().lower()
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for item in jobs:
        if demo and str(item.get("demoId") or "").strip() != demo:
            continue
        if source and str(item.get("dc") or "").strip().upper() != source:
            continue
        if owner_id and str(item.get("owner") or "").strip().lower() != owner_id:
            continue
        score = 0
        item_servers = set(_server_ids(item.get("servers") or []))
        if wanted_servers and item_servers == wanted_servers:
            score += 4
        elif wanted_servers and wanted_servers <= item_servers:
            score += 2
        item_dcs = {d.upper() for d in _dcs_base(item.get("dcs") or [])}
        if wanted_dcs and item_dcs == wanted_dcs:
            score += 3
        elif wanted_dcs and wanted_dcs <= item_dcs:
            score += 1
        try:
            stamp = int(item.get("updateAt") or item.get("at") or 0)
        except (TypeError, ValueError):
            stamp = 0
        ranked.append((score, stamp, item))
    if not ranked:
        return None
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return ranked[0][2]


def submit_camgr_transfer(
    cookie_header: str,
    *,
    site: str,
    saved_id: str,
    servers: list[int],
    dest_dcs: list[str],
    integrate: bool = False,
    integrate_name: str = "",
    integrate_wait: bool = False,
    source_dc: str = "",
) -> dict[str, Any]:
    demo = fetch_camgr_demo(cookie_header, site, saved_id)
    if not demo.get("ok"):
        return demo
    source = (source_dc or str(demo.get("camgrDc") or "")).strip().upper()
    if not source:
        source = site_to_camgr_guid(site)
    server_ids = _server_ids(servers)
    if not server_ids:
        return {"ok": False, "loggedIn": True, "message": "Select at least one VM to transfer."}
    dests = _dest_tokens(dest_dcs, integrate=integrate, integrate_wait=integrate_wait)
    if not dests:
        return {"ok": False, "loggedIn": True, "message": "Select at least one destination DC."}
    payload: dict[str, Any] = {
        "dc": source,
        "demoId": _demo_id_int(str(demo.get("demoId") or saved_id)),
        "servers": server_ids,
        "dcs": dests,
    }
    if integrate:
        payload["flag"] = 0
        payload["name"] = str(integrate_name or "").strip()
        if not payload["name"]:
            return {"ok": False, "loggedIn": True, "message": "Content Integration requires a name."}
    if using_chrome_tab(cookie_header):
        status, body, err = chrome_tab_request("POST", f"{CAMGR_API}/jobs", payload)
        if err:
            return {"ok": False, "loggedIn": True, "message": err}
        if status in {401, 403} or status == 0:
            return _auth_error("CAMGR session expired.")
        if status not in {200, 201}:
            detail = ""
            if isinstance(body, dict):
                detail = str(body.get("message") or body.get("error") or body.get("detail") or "")
            return {
                "ok": False,
                "loggedIn": True,
                "message": detail or f"CAMGR transfer failed (HTTP {status}).",
            }
    else:
        sess = _session(cookie_header)
        sess.headers["Content-Type"] = "application/json"
        try:
            resp = sess.post(f"{CAMGR_API}/jobs", json=payload, timeout=60, allow_redirects=True)
        except requests.RequestException as exc:
            return {"ok": False, "loggedIn": True, "message": describe_request_error(exc, "CAMGR")}
        if _looks_sso(resp) or resp.status_code in {401, 403}:
            return _auth_error("CAMGR session expired.")
        if resp.status_code not in {200, 201}:
            body = _json_body(resp)
            detail = ""
            if isinstance(body, dict):
                detail = str(body.get("message") or body.get("error") or body.get("detail") or "")
            return {
                "ok": False,
                "loggedIn": True,
                "message": detail or f"CAMGR transfer failed (HTTP {resp.status_code}).",
            }
    listed = list_camgr_jobs(cookie_header)
    if listed.get("loggedIn") is False:
        return listed
    hit = match_camgr_job(
        listed.get("jobs") or [],
        demo_id=str(demo.get("demoId") or saved_id),
        source_dc=source,
        servers=server_ids,
        dest_dcs=dests,
        owner=str(demo.get("owner") or ""),
    )
    pub = public_camgr_job(hit)
    return {
        "ok": True,
        "loggedIn": True,
        "message": "CAMGR transfer submitted.",
        "camgrDc": source,
        "demoId": str(demo.get("demoId") or saved_id),
        "servers": server_ids,
        "dcs": dests,
        "job": pub,
        "guid": pub.get("guid") or "",
        "status": pub.get("status") or "ready",
        "statusRaw": pub.get("statusRaw") or "READY",
        "progress": pub.get("progress") or 0,
    }


def submit_camgr_vpod_transfer(
    cookie_header: str,
    *,
    source_guid: str,
    vpod: str,
    servers: list[int],
    dest_dcs: list[str],
) -> dict[str, Any]:
    """Transfer out of a ContentDEV vPod. CAMGR reuses the demo slot for the vPod id."""
    source = str(source_guid or "").strip().upper()
    vpod_id = str(vpod or "").strip()
    if not source or not vpod_id:
        return {"ok": False, "loggedIn": True, "message": "Pick a ContentDEV datacenter and a vPod."}
    server_ids = _server_ids(servers)
    if not server_ids:
        return {"ok": False, "loggedIn": True, "message": "Select at least one VM to transfer."}
    home = camgr_cdev_home_dc(source)
    dests = _dest_tokens(dest_dcs, integrate=False, integrate_wait=False)
    if not dests:
        return {"ok": False, "loggedIn": True, "message": "Select at least one destination DC."}
    if home:
        stray = [dc for dc in dests if _dc_name(dc).upper() != home]
        if stray:
            return {
                "ok": False,
                "loggedIn": True,
                "message": f"{source} can only transfer back to {home}, not {', '.join(stray)}.",
            }
    payload: dict[str, Any] = {
        "dc": source,
        "demoId": _demo_id_int(vpod_id),
        # CAMGR's ContentDEV form posts vPod VM IDs as JSON strings.
        "servers": [str(server_id) for server_id in server_ids],
        "dcs": dests,
    }
    if using_chrome_tab(cookie_header):
        status, body, err = chrome_tab_request("POST", f"{CAMGR_API}/jobs", payload)
        if err:
            return {"ok": False, "loggedIn": True, "message": err}
        if status in {401, 403} or status == 0:
            return _auth_error("CAMGR session expired.")
        if status not in {200, 201}:
            detail = ""
            if isinstance(body, dict):
                detail = str(body.get("message") or body.get("error") or body.get("detail") or "")
            return {
                "ok": False,
                "loggedIn": True,
                "message": detail or f"CAMGR transfer failed (HTTP {status}).",
                "payload": payload,
            }
    else:
        sess = _session(cookie_header)
        sess.headers["Content-Type"] = "application/json"
        try:
            resp = sess.post(f"{CAMGR_API}/jobs", json=payload, timeout=60, allow_redirects=True)
        except requests.RequestException as exc:
            return {"ok": False, "loggedIn": True, "message": describe_request_error(exc, "CAMGR")}
        if _looks_sso(resp) or resp.status_code in {401, 403}:
            return _auth_error("CAMGR session expired.")
        if resp.status_code not in {200, 201}:
            body = _json_body(resp)
            detail = ""
            if isinstance(body, dict):
                detail = str(body.get("message") or body.get("error") or body.get("detail") or "")
            return {
                "ok": False,
                "loggedIn": True,
                "message": detail or f"CAMGR transfer failed (HTTP {resp.status_code}).",
                "payload": payload,
            }
    listed = list_camgr_jobs(cookie_header)
    if listed.get("loggedIn") is False:
        return listed
    hit = match_camgr_job(
        listed.get("jobs") or [],
        demo_id=vpod_id,
        source_dc=source,
        servers=server_ids,
        dest_dcs=dests,
    )
    pub = public_camgr_job(hit)
    return {
        "ok": True,
        "loggedIn": True,
        "message": "CAMGR transfer submitted.",
        "camgrDc": source,
        "vpod": vpod_id,
        "demoId": vpod_id,
        "servers": server_ids,
        "dcs": dests,
        "payload": payload,
        "job": pub,
        "guid": pub.get("guid") or "",
        "status": pub.get("status") or "ready",
        "statusRaw": pub.get("statusRaw") or "READY",
        "progress": pub.get("progress") or 0,
    }


def refresh_camgr_job(
    cookie_header: str,
    *,
    guid: str = "",
    demo_id: str = "",
    source_dc: str = "",
    servers: list[int] | None = None,
    dest_dcs: list[str] | None = None,
    owner: str = "",
) -> dict[str, Any]:
    listed = list_camgr_jobs(cookie_header)
    if not listed.get("ok"):
        return listed
    jobs = listed.get("jobs") or []
    hit = match_camgr_job(
        jobs,
        guid=guid,
        demo_id=demo_id,
        source_dc=source_dc,
        servers=servers,
        dest_dcs=dest_dcs,
        owner=owner,
    )
    if hit is None and (source_dc or demo_id):
        extra = list_camgr_jobs(
            cookie_header,
            only_not_status="",
            only_user=None,
            only_dc=source_dc,
            only_demo=str(demo_id or ""),
        )
        if extra.get("loggedIn") is False:
            return extra
        if extra.get("ok"):
            hit = match_camgr_job(
                extra.get("jobs") or [],
                guid=guid,
                demo_id=demo_id,
                source_dc=source_dc,
                servers=servers,
                dest_dcs=dest_dcs,
                owner=owner,
            )
    if hit is None:
        return {"ok": True, "loggedIn": True, "found": False, "job": {}}
    return {"ok": True, "loggedIn": True, "found": True, "job": public_camgr_job(hit)}
