#!/usr/bin/env python3
"""Fast regression checks for the bugs this tool has actually shipped.

Maintainer-only: not packed into coworker zips and skipped by the updater.
Nothing here talks to dCloud, CAI, CAMGR, or GitHub, so a full run is about a
second. It is not a product test — it guards the specific rules we keep
breaking. Run it before a push:

    .venv/bin/python tests/check.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

FAILURES: list[str] = []
PASSED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        return
    FAILURES.append(f"{name}{f' — {detail}' if detail else ''}")


def js_function(name: str) -> str:
    """Pull one function out of index.html so node can run it on its own."""
    start = INDEX.index(f"function {name}(")
    depth = 0
    opened = False
    for i in range(start, len(INDEX)):
        if INDEX[i] == "{":
            depth += 1
            opened = True
        elif INDEX[i] == "}":
            depth -= 1
            if opened and depth == 0:
                return INDEX[start : i + 1]
    raise AssertionError(f"could not read function {name} out of index.html")


def run_node(source: str, label: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(source)
        path = handle.name
    try:
        result = subprocess.run([sys.executable and "node", path], capture_output=True, text=True)
        check(label, result.returncode == 0, (result.stderr or result.stdout).strip()[:400])
    finally:
        Path(path).unlink(missing_ok=True)


# --------------------------------------------------------------------------
# The page still parses, and the backend still imports.
# --------------------------------------------------------------------------

def test_sources_parse() -> None:
    scripts = re.findall(r"<script>(.*?)</script>", INDEX, re.S)
    check("index.html has inline script", bool(scripts))
    for num, script in enumerate(scripts, 1):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(script)
            path = handle.name
        try:
            result = subprocess.run(["node", "--check", path], capture_output=True, text=True)
            check(f"script block {num} parses", result.returncode == 0, result.stderr.strip()[:400])
        finally:
            Path(path).unlink(missing_ok=True)


def test_root_id_is_not_the_target() -> None:
    """Target is the previous save. Root is the original base from CAMGR."""
    import app
    from dcloud_client import (
        extract_parent_content_id,
        extract_root_content_id,
        root_content_id_is_self,
    )

    # dCloud spells it _rootDemoId; CAMGR uses fkrootDemoId. Missing the dCloud
    # spelling left Root blank on rows dCloud could answer without CAMGR.
    dcloud_payload = {"_rootDemoId": 1207125, "pkdemoId": 1391854}
    check(
        "a dCloud root is read",
        extract_root_content_id(dcloud_payload, saved_id="1391854") == "1207125",
    )
    check(
        "a dCloud self-root is an answer, not a blank",
        root_content_id_is_self({"_rootDemoId": 483886}, saved_id="483886"),
    )
    check(
        "a root is never read as the parent",
        extract_parent_content_id(dcloud_payload, saved_id="1391854") == "",
    )
    check(
        "a lookup from an older version is retried once",
        "rootLookupVersion" in app.ROOT_FIELDS and app.ROOT_LOOKUP_VERSION >= 3,
    )
    # CAMGR uses either spelling depending on the demo, and reading only
    # fkrootDemoId left Root blank on demos that answer with _rootDemoId.
    import camgr_client

    check(
        "CAMGR's _rootDemoId is read",
        camgr_client._root_demo_id({"pkdemoId": 1391854, "_rootDemoId": 1207125}) == "1207125",
    )
    check(
        "CAMGR's fkrootDemoId still works",
        camgr_client._root_demo_id({"fkrootDemoId": "1387783"}) == "1387783",
    )
    check(
        "an unrelated ID is not read as the root",
        camgr_client._root_demo_id({"fkownerId": "jasmurra", "pkdemoId": 1391854}) == "",
    )

    # A saved card in the job produced a second row for the same ID with no root
    # fields, and it came first — so the stored Root ID was dropped as a dupe.
    summary = (ROOT / "app.py").read_text(encoding="utf-8")
    body = summary[summary.index("def _saved_id_summary(") : summary.index("def _persist_saved_ids(")]
    check(
        "stored rows are read before the job copies",
        body.index("sources: list[dict[str, Any]] = list(_managed_saved_state()")
        < body.index("for row in auto_add:"),
    )
    check("a job copy only fills an ID with no stored row", "not in stored_keys" in body)

    payload = {"parentId": "222", "fkrootDemoId": 111}
    check(
        "parent is the previous save",
        extract_parent_content_id(payload, saved_id="333") == "222",
    )
    check(
        "root is the original base",
        extract_root_content_id(payload, saved_id="333") == "111",
    )
    check(
        "root is not used as parent",
        extract_parent_content_id({"fkrootDemoId": "111"}, saved_id="333") == "",
    )

    source = (ROOT / "app.py").read_text(encoding="utf-8")
    start = source.index("def _lookup_published_id(")
    body = source[start : source.index("def _lookup_root_id(")]
    check("target lookup does not call CAMGR", "fetch_camgr_demo" not in body)
    check("root lookup lives next to target lookup", "def _lookup_root_id(" in source)
    check("connect fills missing roots", "/api/saved-ids/lookup-roots" in source)

    # A failed CAMGR call must not mark the row done, or Root stays blank forever.
    check(
        "the root lookup reports whether anything answered",
        "def _lookup_root_id(site: str, saved_id: str) -> RootLookup:" in source,
    )
    check("a self-root is its own answer", "is_self: bool = False" in source)
    check("a recheck can ignore the done flag", "def _backfill_root_demo_ids(*, force: bool = False)" in source)
    check(
        "an empty root can overwrite a stored one",
        all(field in app.ROOT_FIELDS for field in ("rootDemoId", "rootLookupDone", "rootNote"))
        and "overwrite_keys=ROOT_FIELDS" in source,
    )
    check(
        "a CAMGR name no longer marks the root done",
        'bool(root) or bool(name)' not in source,
    )

    check("the Hub table has a Root ID column", "<th>Root ID</th>" in INDEX)
    check("a blank root explains itself", "row.rootNote" in INDEX)
    check("roots can be rechecked", "btn-recheck-roots" in INDEX)
    # A base demo shows its own ID, and copying it into Target would replace itself.
    check("a base demo shows its own ID as the root", "rootIsSelf" in INDEX)
    check(
        "a base demo gets no copy button",
        INDEX.index("const rootIsSelf") < INDEX.index("rootDiffers ? ` <button"),
    )
    check("a blank Root column tells people to connect CAMGR", "saved-ids-root-hint" in INDEX)
    check("Use root as target is on the toolbar", "btn-use-root-as-target" in INDEX)
    check("a row can copy root into target", "btn-use-root" in INDEX)


def test_release_metadata() -> None:
    import app
    from update_from_github import is_newer

    check("app.py reports the VERSION file", app._read_app_version() == VERSION)
    check("VERSION looks like a release", bool(re.fullmatch(r"\d+(\.\d+)+", VERSION)), VERSION)
    check("a newer VERSION wins", is_newer(VERSION, "0.9"))
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    check(f"CHANGELOG has a {VERSION} section", f"## {VERSION}" in changelog)
    check(
        "CHANGELOG starts at a version, not a title",
        changelog.lstrip().startswith("## "),
        changelog.splitlines()[0] if changelog.strip() else "(empty)",
    )
    notes = app._changelog_notes(
        "# What’s new\n\nNewest version first.\n\n## 1.10 — 2026-09-16\n- Delay\n"
    )
    check("What’s new skips the preamble", notes.startswith("## 1.10"), notes[:40])
    check("What’s new does not repeat the title", "Newest version first" not in notes)
    check("What’s new turns headings into HTML", "function changelogHtml(" in INDEX)
    check("What’s new does not dump raw markdown", '<pre class="changelog-text">' not in INDEX)
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    check("the header reads VERSION from disk", '"version": _read_app_version()' in source)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    check(f"README What's new lists {VERSION}", f"**{VERSION}**" in readme)


def test_zip_contents() -> None:
    """The 'ModuleNotFoundError: net_errors' class of bug: a file the app needs
    but the packer never puts in the coworker zip."""
    import pack_for_mac
    import update_from_github

    for rel in pack_for_mac.FILES:
        check(f"packed file exists: {rel}", (ROOT / rel).is_file())
    for maintainer_only in (
        "pack_for_mac.py",
        "share-for-mac.command",
        "share-for-mac-with-python.command",
        "collect_usage.py",
        "show_usage.py",
    ):
        check(
            f"updater skips maintainer file: {maintainer_only}",
            maintainer_only in update_from_github.SKIP_FILE_NAMES,
        )
    check("updater skips this tests folder", "tests" in update_from_github.SKIP_DIR_NAMES)
    check("updater skips GitHub workflows", ".github" in update_from_github.SKIP_DIR_NAMES)
    check("install id stays on the machine", ".dcloud-install.json" in update_from_github.SKIP_FILE_NAMES)
    check("the -update zip is the overlay without Python", pack_for_mac.ZIP_NAME_UPDATE.endswith("-update.zip"))
    check("the -full zip is the first-time install with Python", pack_for_mac.ZIP_NAME_FULL.endswith("-full.zip"))


def test_anonymous_update_usage() -> None:
    import hashlib
    import json
    import tempfile
    from pathlib import Path

    import collect_usage
    import update_from_github

    posted: list[bytes] = []

    def post(request):
        posted.append(request.data)
        class _Resp:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
        return _Resp()

    with tempfile.TemporaryDirectory() as tmp:
        install = Path(tmp) / ".dcloud-install.json"
        real_install = update_from_github.INSTALL_FILE
        update_from_github.INSTALL_FILE = install
        try:
            ok = update_from_github.record_anonymous_usage(
                version="1.12.6",
                config={"usage_topic": "dcloud-cm-usage-a8f31c9e"},
                force=True,
                post=post,
            )
            check("a usage ping is sent", ok and len(posted) == 1)
            payload = json.loads(posted[0].decode())
            check("the ping has no name fields", set(payload) <= {"id", "version"})
            check("the ping version is the app version", payload["version"] == "1.12.6")
            ident = str(payload["id"])
            check("the install id is a short hash", len(ident) == 16 and ident.isalnum())
            raw = json.loads(install.read_text())["id"]
            check(
                "the raw uuid is not what is sent",
                ident != raw and ident == hashlib.sha256(f"dcloud-content-manager:{raw}".encode()).hexdigest()[:16],
            )
            skipped = update_from_github.record_anonymous_usage(
                version="1.12.6",
                config={"usage_topic": "dcloud-cm-usage-a8f31c9e"},
                now=float(json.loads(install.read_text())["last_ping"]) + 60,
                post=post,
            )
            check("pings are throttled", skipped is False and len(posted) == 1)
        finally:
            update_from_github.INSTALL_FILE = real_install

    data = {"v": 1, "installs": {}}
    collect_usage.merge_ping(data, {"id": "0123456789abcdef", "version": "1.12.6", "seen": "2026-09-19T12:00:00Z"})
    collect_usage.merge_ping(data, {"id": "not-a-hash", "version": "1.12.6"})
    check("only hashed ids are stored", list(data["installs"]) == ["0123456789abcdef"])
    check("github-update names a usage topic", "usage_topic=" in (ROOT / "github-update.txt").read_text())


# --------------------------------------------------------------------------
# Backend rules (1.8.2, 1.8.4, 1.8.5)
# --------------------------------------------------------------------------

def test_shared_with_survives_status_poll() -> None:
    """1.8.2: the poll fetches expand=server, which carries no sharing at all."""
    import app

    poll = {"status": "2", "expand": {"server": {}}}
    check("status poll leaves sharing alone", app._shared_with_update(poll) == {})
    shared = {"expand": {"sharedWith": [{"userId": "bob@cisco.com", "fullName": "Bob"}]}}
    check(
        "an expanded response updates sharing",
        app._shared_with_update(shared) == {"sharedWith": [{"userId": "bob@cisco.com", "fullName": "Bob"}]},
    )
    check(
        "an explicit empty list still clears sharing",
        app._shared_with_update({"expand": {"sharedWith": []}}) == {"sharedWith": []},
    )

    dc = {"site": "rtp", "sessionId": "1356727", "sharedWith": [{"userId": "bob@cisco.com", "fullName": "Bob"}]}
    dc.update(app._shared_with_update(poll), status="Active")
    check("a polled card keeps its people", dc["sharedWith"][0]["fullName"] == "Bob")

    # The helper is useless if the poll stops calling it, so pin the call site.
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    check("the poll goes through the guard", "**_shared_with_update(details)," in source)
    check(
        "the poll never writes sharing straight from a server expand",
        "sharedWith=shared_with_from_details(details)" not in source,
    )


def test_live_session_share_search_uses_dsx() -> None:
    """dCloud's live-session share box searches DSX by default. The tool used to
    search all cisco.com users instead, which missed partner emails on SJC/SNG/SYD."""
    import dcloud_client

    partner = {
        "userId": "00uvquferzzlcmsgu5d7",
        "fullName": "MARK CUPID",
        "email": "mcupid@vqcomms.com",
    }

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    real_request = dcloud_client._request
    try:
        calls: list[str] = []

        def dsx_hit(method, url, token, **kwargs):
            calls.append(url)
            if "scope=dsx" in url:
                return FakeResponse(200, {"success": True, "users": [partner]})
            return FakeResponse(200, {"success": True, "users": []})

        dcloud_client._request = dsx_hit
        users, err = dcloud_client.search_share_users(
            "token", "sjc", "mcupid@vqcomms.com", content_scope=False
        )
        check("live session search does not error", err is None, str(err))
        check(
            "live session search asks DSX first",
            calls == [
                "https://dcloud2-sjc.cisco.com/api/users/search?name=mcupid%40vqcomms.com&scope=dsx"
            ],
            str(calls),
        )
        check(
            "a DSX partner is returned without a cisco.com follow-up",
            users == [{
                "userId": partner["userId"],
                "fullName": partner["fullName"],
                "email": partner["email"],
            }],
            str(users),
        )

        calls.clear()

        def empty_dsx(method, url, token, **kwargs):
            calls.append(url)
            if "scope=dsx" in url:
                return FakeResponse(200, {"success": True, "users": []})
            return FakeResponse(200, {"success": True, "users": [{
                "userId": "cisco-user",
                "fullName": "Cisco User",
                "email": "user@cisco.com",
            }]})

        dcloud_client._request = empty_dsx
        users, err = dcloud_client.search_share_users(
            "token", "sjc", "user@cisco.com", content_scope=False
        )
        check(
            "live sessions fall back to all cisco.com users if DSX is empty",
            err is None
            and len(users) == 1
            and users[0]["userId"] == "cisco-user"
            and len(calls) == 2
            and "scope=dsx" in calls[0]
            and "scope=" not in calls[1].split("?")[-1],
            str(calls),
        )

        calls.clear()
        users, err = dcloud_client.search_share_users(
            "token", "sjc", "nobody@cisco.com", content_scope=True
        )
        check(
            "saved content stays on DSX only",
            err is None and users == [] and len(calls) == 1 and "scope=dsx" in calls[0],
            str(calls),
        )
    finally:
        dcloud_client._request = real_request

    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    check(
        "the share dialog describes the DSX-first search",
        "DSX users first" in page and "Live sessions search all users; saved content uses DSX" not in page,
    )


def test_owner_line_survives_a_tokenless_render() -> None:
    """last-job.json holds no token, so a restored job used to answer "I cannot
    tell who owns this" for every card — and persist that blank."""
    import base64
    import json as _json

    import app

    def fake_token(ccoid: str) -> str:
        claims = base64.urlsafe_b64encode(_json.dumps({"ccoid": ccoid}).encode()).decode().rstrip("=")
        return f"e30.{claims}.unsigned-test-token"

    job = {"id": "j1", "token": "", "dcs": [{"site": "sjc", "sessionId": "491381", "owner": "dimena"}]}
    saved_auth = app._user_auth.get("access_token")
    try:
        app._user_auth["access_token"] = fake_token("jasmurra")
        app._annotate_dc_ownership(job)
        check(
            "a restored job still knows a session is someone else's",
            job["dcs"][0]["ownedByMe"] is False,
        )

        job["dcs"][0]["owner"] = "jasmurra"
        app._annotate_dc_ownership(job)
        check("my own session reads as mine", job["dcs"][0]["ownedByMe"] is True)

        # Signed out entirely: keep the last answer instead of blanking the card.
        app._user_auth["access_token"] = ""
        job["dcs"][0]["ownedByMe"] = False
        app._annotate_dc_ownership(job)
        check(
            "an unknown identity does not erase Owned by",
            job["dcs"][0]["ownedByMe"] is False,
        )
    finally:
        app._user_auth["access_token"] = saved_auth

    check(
        "the owner line is driven by ownedByMe",
        "dc.ownedByMe === false" in INDEX and "Owned by ${escapeHtml(dc.owner" in INDEX,
    )


def test_session_card_shows_virtual_center() -> None:
    """The dashboard's Virtual Center number belongs on the session card."""
    from dcloud_client import session_virtual_center
    import app

    check(
        "list payloads use virtualCenter",
        session_virtual_center({"virtualCenter": 5}) == "5",
    )
    check(
        "session details use virtualCenterId",
        session_virtual_center({"virtualCenterId": "5"}) == "5",
    )
    check(
        "a status poll with no VC does not invent one",
        session_virtual_center({"status": "2", "expand": {"server": {}}}) == "",
    )
    fields = app._dc_ids_from_session({"status": "2", "expand": {"server": {}}})
    check("a poll without VC does not clear the card", "virtualCenter" not in fields)
    fields = app._dc_ids_from_session({"virtualCenter": 5, "parentId": "1376509"})
    check("a payload with VC stamps the card", fields.get("virtualCenter") == "5")
    check("the card template shows Virtual Center", "Virtual Center ${escapeHtml(virtualCenter)}" in INDEX)


def test_shutdown_save_waits_for_vms_to_power_off() -> None:
    """Card Shutdown & save used to POST save as soon as dCloud accepted guest
    shutdown. It now waits until those VMs actually show powered off."""
    import app
    import dcloud_client

    msg = app._shutdown_wait_message([{"name": "CUCM"}, {"displayName": "IMP"}])
    check("the card names VMs still shutting down", "CUCM" in msg and "IMP" in msg)
    check("the card says it is waiting to save", "Waiting for VMs to shut down before saving" in msg)

    merged = app._merge_observed_vm_power(
        [
            {"name": "CUCM", "powerState": "Powered On"},
            {"name": "IMP", "powerState": "Powered On"},
        ],
        [
            {"name": "CUCM", "powerState": "Powered Off"},
            {"name": "IMP", "powerState": "Powered On"},
        ],
        [{"name": "IMP", "powerState": "Powered On"}],
    )
    check("a verified VM leaves the pending list", merged[0]["powerState"] == "Powered Off")
    check("a live VM stays marked as shutting down", merged[1]["shutdownPending"] is True)

    vcube_msg = app._shutdown_wait_message([{"name": "VCUBE"}, {"name": "CUCM"}])
    check("vCUBE is called out as a power-off", "Powering off (no guest shutdown): VCUBE" in vcube_msg)
    check("other VMs still wait for guest shutdown", "CUCM" in vcube_msg)

    import dcloud_client
    check("vCUBE is detected", dcloud_client.vm_needs_hard_power_off({"name": "VCUBE"}) is True)
    check("v-CUBE is detected", dcloud_client.vm_needs_hard_power_off({"displayName": "Cisco v-CUBE"}) is True)
    check("CUCM is not forced off", dcloud_client.vm_needs_hard_power_off({"name": "CUCM"}) is False)
    check("CUCM is treated as a slow guest shutdown", dcloud_client.vm_is_slow_guest_shutdown({"name": "CUCM-PUB"}) is True)
    check("Unity Connection is treated as slow", dcloud_client.vm_is_slow_guest_shutdown({"name": "CUC"}) is True)
    check("vCUBE is not treated as a slow guest", dcloud_client.vm_is_slow_guest_shutdown({"name": "VCUBE"}) is False)
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    start = source.index("def _shutdown_one_dc(")
    body = source[start : source.index("def _shutdown_job(")]
    wait_fn = source[source.index("def _wait_until_vms_off(") : source.index("def _vm_power_summary(")]
    check("the save wait uses the shared shutdown waiter", "_wait_until_vms_off(" in body)
    check(
        "accepted guest shutdowns are never force-powered-off later",
        "still on after 5 minutes — powering off" not in body and "grace_seconds = 5 * 60" not in body,
    )
    check(
        "slow UC shutdown waits without a final timeout",
        'while not waited.get("ok") and not job["stop"].is_set()' in wait_fn,
    )
    check(
        "the wait asks after 10 minutes instead of yanking VMs",
        "SHUTDOWN_PROMPT_SECONDS = 10 * 60" in source
        and "_set_shutdown_prompt(" in wait_fn
        and "dc[\"shutdownPrompt\"]" in source,
    )
    check(
        "the save never starts after a shutdown timeout",
        "Starting save anyway" not in body,
    )
    long_msg = app._long_shutdown_wait_message(
        [{"name": "CUCM"}, {"name": "WIN-DC"}]
    )
    check(
        "the card promises not to power off slow UC",
        "will not be powered off: CUCM" in long_msg,
    )
    check(
        "the card still names other pending VMs",
        "WIN-DC" in long_msg,
    )
    guest_src = (ROOT / "dcloud_client.py").read_text(encoding="utf-8")
    guest_fn = guest_src[
        guest_src.index("def guest_shutdown_vms(") : guest_src.index("def list_dashboard_sessions(")
    ]
    check("vCUBE skips guest shutdown", "vm_needs_hard_power_off(vm)" in guest_fn)
    check("vCUBE is powered off instead", '"vmPowerOff"' in guest_fn and "reason" in guest_fn)
    check("the card hides Guest shutdown on vCUBE", "vmNeedsHardPowerOff(vm)" in INDEX)
    check("the API rewrites vCUBE guest shutdown", "vCUBE has no guest shutdown" in (ROOT / "app.py").read_text(encoding="utf-8"))
    check(
        "the card can shut down all powered-on VMs without saving",
        "Guest shutdown all powered-on VMs" in INDEX,
    )
    check(
        "the no-save action has its own endpoint",
        "/guest-shutdown-all" in INDEX and '"/api/jobs/{job_id}/guest-shutdown-all"' in source,
    )
    check(
        "manual shutdown keeps checking until all VMs are off",
        "def _guest_shutdown_all_worker(" in source
        and "All VMs are powered off. No save was submitted" in source,
    )
    check(
        "the page keeps polling while manual shutdown is monitored",
        "jobHasManualShutdownInProgress" in INDEX,
    )
    check(
        "a 10-minute shutdown wait can keep waiting or power off",
        '"/api/jobs/{job_id}/shutdown-choice"' in source
        and "Keep waiting" in INDEX
        and "Power off remaining VMs" in INDEX
        and "shutdown-wait-alert" in INDEX,
    )
    check(
        "hard power-off of leftover VMs is only after the user chooses it",
        'choice == "power_off"' in wait_fn and "vmPowerOff" in wait_fn,
    )

    source = (ROOT / "app.py").read_text(encoding="utf-8")
    start = source.index("def _shutdown_one_dc(")
    body = source[start : source.index("def _shutdown_job(")]
    check(
        "save is after the powered-off wait",
        body.index("_wait_until_vms_off(") < body.index("save_session("),
    )
    check("the wait is for powered off", "want_on=False" in wait_fn)
    check(
        "the old fire-and-save message is gone",
        "dCloud handles shutdown on save" not in body,
    )
    # Bulk Guest shutdown & save has to reuse the same worker, or only the card
    # button would wait.
    bulk = source[source.index("def _shutdown_job(") : source.index("def _end_job(")]
    check("bulk save goes through the same wait", "_shutdown_one_dc," in bulk)

    # Card actions name the action and drop the redundant "session".
    check('the card menu says "Guest shutdown & save"', '"Guest shutdown &amp; save"' in INDEX)
    check("the old card label is gone", "Shutdown &amp; save this session" not in INDEX)
    for gone in ("Share session…", ">Reset session<"):
        check(f"card menu no longer says {gone}", gone not in INDEX)
    check(
        "end and cancel labels lose the word session",
        '"Cancel (not yours)" : "Cancel"' in INDEX and '"End (not yours)" : "End"' in INDEX,
    )

    seen: list[tuple[int, int]] = []
    real_list = dcloud_client.list_session_vms
    real_tbv3 = dcloud_client.apply_tbv3_power_states
    try:
        dcloud_client.list_session_vms = lambda *a, **k: (
            [{"name": "CUCM", "mor": "m1", "powerState": "Powered Off"}],
            {},
            None,
        )
        dcloud_client.apply_tbv3_power_states = lambda *a, **k: (a[3], None)
        out = dcloud_client.wait_for_power_state(
            "t",
            "rtp",
            "1",
            [{"name": "CUCM", "mor": "m1"}],
            want_on=False,
            timeout_seconds=5,
            poll_seconds=0,
            on_wait=lambda selected, pending: seen.append((len(selected), len(pending))),
        )
        check("on_wait reports no VMs still on", seen == [(1, 0)], str(seen))
        check("wait_for_power_state succeeds when off", out.get("ok") is True)
    finally:
        dcloud_client.list_session_vms = real_list
        dcloud_client.apply_tbv3_power_states = real_tbv3


def test_no_stale_step_numbers() -> None:
    """Sign-in moved to a header button, so "Step 1" points at nothing."""
    for rel in ("app.py", "dcloud_client.py", "browser_auth/browser_dcloud_auth.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        check(f"{rel} has no step numbers", not re.search(r"Step [123]", text))
    check(
        "the expired hint names the button",
        "Sign in to dCloud at the top of the page" in (ROOT / "app.py").read_text(encoding="utf-8"),
    )


def test_token_refresh_is_not_raced() -> None:
    """dCloud retires the old refresh token, so two threads refreshing at once
    made the loser report "signed out" under a green Sign in button."""
    import app

    source = (ROOT / "app.py").read_text(encoding="utf-8")
    body = source[
        source.index("def _ensure_user_access_token(") : source.index("def _refresh_with_any_site(")
    ]
    check("only one refresh runs at a time", "with _token_refresh_lock:" in body)
    check(
        "the session is re-read after waiting for the lock",
        body.count("_read_user_session()") >= 2,
    )
    check(
        "the lock covers the refresh call, not just the dict",
        "_token_refresh_lock = threading.Lock()" in source,
    )
    check("a live token is reused", app._access_token_is_usable("t", 0) is True)
    check("an expired token is not reused", app._access_token_is_usable("t", 1.0) is False)
    check("no token is never usable", app._access_token_is_usable("", 0) is False)
    soon = time.time() + 60
    check(
        "a token inside the 5-minute window is refreshed early",
        app._access_token_is_usable("t", soon) is False,
    )
    later = time.time() + 20 * 60
    check("a token with plenty of life is reused", app._access_token_is_usable("t", later) is True)
    keepalive = source[
        source.index("def _auth_keepalive_loop(") : source.index("def _start_auth_keepalive(")
    ]
    check("keepalive refreshes the dCloud token too", '"dcloud": _dcloud_keepalive' in keepalive)
    check(
        "jobs use the live session, not a copied refresh token",
        "_ensure_user_access_token(progress, force=force)" in source,
    )
    check("the page pings auth every 4 minutes", "4 * 60 * 1000" in INDEX)
    # A failed call has to correct the button, not wait for the next poll.
    check("a sign-in failure re-checks auth", "refreshAuth().catch(() => {});" in INDEX)


def test_schedule_button_reports_back() -> None:
    """With Job workspace collapsed, the click had no visible result at all."""
    check("the button has a result line", 'id="run-result"' in INDEX)
    check("screen readers hear it too", 'role="status"' in INDEX and 'aria-live="polite"' in INDEX)
    check("the result is cleared on the next click", 'setRunResult("");' in INDEX)
    check("the job render keeps it current", "updateRunResult(job);" in INDEX)
    check("it counts cards that existed before", "before.get(runTargetKey(" in INDEX)
    check("it names the sessions it got", "→ session ${ids.map(escapeHtml).join" in INDEX)
    check("it says when a card never scheduled", "not scheduled (${missing} of" in INDEX)
    check("it can open the collapsed workspace", 'goToPageTarget("#panel-job-cards")' in INDEX)
    check("it stops updating once settled", "runWatch.settled = true;" in INDEX)


def test_resubmitted_transfer_is_followed() -> None:
    """A retry is a new CAMGR job. The row used to stay stuck on the old ERROR."""
    import app
    from camgr_client import match_camgr_job

    failed = {
        "guid": "job-22",
        "demoId": "1389488",
        "dc": "RTP",
        "owner": "mgianni",
        "status": "ERROR",
        "dcs": ["RTP", "SJC"],
        "servers": list(range(22)),
        "updateAt": 1_000,
    }
    retry = {
        "guid": "job-23",
        "demoId": "1389488",
        "dc": "RTP",
        "owner": "mgianni",
        "status": "IMPORTING",
        "dcs": ["RTP", "SJC"],
        "servers": list(range(23)),
        "updateAt": 2_000,
    }
    args = {
        "demo_id": "1389488",
        "source_dc": "RTP",
        "dest_dcs": ["RTP", "SJC"],
        "owner": "mgianni",
    }
    # The row is still pointed at the failed job, which is what Mario hit.
    hit = match_camgr_job([failed, retry], guid="job-22", **args)
    check("a live retry wins over the failed job it replaced", hit is retry, str(hit))
    # A job that is still running is never second-guessed.
    running = dict(failed, status="XFRING")
    hit = match_camgr_job([running, retry], guid="job-22", **args)
    check("a running job stays matched by guid", hit is running)
    # Nothing newer to move to, so keep reporting the failure.
    hit = match_camgr_job([failed], guid="job-22", **args)
    check("a lone failure is still reported", hit is failed)
    older = dict(retry, guid="job-21", updateAt=10, status="IMPORTING")
    hit = match_camgr_job([failed, older], guid="job-22", **args)
    check("an older job is not adopted", hit is failed)

    now = time.time()
    check(
        "a failed row asks CAMGR again",
        app._camgr_error_row_is_due({"status": "error", "errorCheckedAt": 0}) is True,
    )
    check(
        "it does not ask on every poll",
        app._camgr_error_row_is_due({"status": "error", "errorCheckedAt": now}) is False,
    )
    check(
        "a completed row is left alone",
        app._camgr_error_row_is_due({"status": "complete", "errorCheckedAt": 0}) is False,
    )
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    check(
        "the log says it switched to the retry",
        "following the re-submitted CAMGR" in source,
    )


def test_failed_schedule_leaves_no_card() -> None:
    """A card with no session is nothing to act on, so it should not need clearing."""
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    body = source[source.index("def _schedule_one_dc(") : source.index("def _parallel_schedule_pending(")]
    check("a failed schedule drops its card", body.count("_drop_dc_card(") >= 2)
    check(
        "the reason still reaches the log and banner",
        '_log(job, f"{site}: not scheduled' in source and 'job["error"]' in source,
    )
    # A capacity conflict keeps its card: that one has Adjust schedule on it.
    conflict = body[body.index('if result.get("conflict"):') : body.index("_drop_dc_card(job, dc, str(")]
    check("a capacity conflict keeps its card", "scheduleConflict=availability" in conflict)
    check("the conflict card is not dropped", "_drop_dc_card(" not in conflict)


def test_active_card_says_nothing() -> None:
    """A healthy Active card carried a "connect, then guest-shutdown & save"
    line. The buttons say that already."""
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    check("the connect hint is gone", "Connect to your session" not in source)
    check("its helper is gone too", "_active_card_message" not in source)
    check("an empty message leaves no empty row", "${dc.message ? `<div" in INDEX)


def test_shutdown_wait_message_is_not_overwritten() -> None:
    """The card flipped between the pending VM list and "waiting for dCloud to
    report the save" — a save that had not been submitted yet."""
    import app

    source = (ROOT / "app.py").read_text(encoding="utf-8")
    poll = source[
        source.index("def _refresh_dc_save_progress(") : source.index("def _dc_reset_in_progress(")
    ]
    check("the poll respects the running wait", '_shutdown_waiting' in poll)
    check(
        "the poll cannot rewrite the card the wait owns",
        'for owned in ("message", "phase", "savePending"):' in poll,
    )

    job = {"id": "j1", "discarded": True, "dcs": [], "log": []}
    dc = {
        "site": "rtp",
        "sessionId": "1356727",
        "phase": "shutting_down",
        "message": "Waiting for VMs to shut down before saving: CUCM",
        "_shutdown_waiting": True,
    }
    job["dcs"] = [dc]
    wait_owns = bool(dc.get("_shutdown_waiting"))

    def bump(**fields):
        if wait_owns and str(fields.get("phase") or "") in {"shutting_down", "saving"}:
            for owned in ("message", "phase", "savePending"):
                fields.pop(owned, None)
        app._set_dc(job, "rtp", match_session="1356727", **fields)

    bump(phase="saving", savePending=True, message="Waiting for dCloud to report the save.")
    check(
        "a save poll leaves the VM list on the card",
        dc["message"].startswith("Waiting for VMs to shut down"),
        dc["message"],
    )
    bump(phase="save_failed", message="Save failed or session error (ERROR).")
    check("a failed save still speaks up", dc["message"].startswith("Save failed"), dc["message"])

    # A restart kills the wait thread, so the flag must not outlive it.
    restored = app._hydrate_job({"id": "j1", "dcs": [dict(dc)]})
    for item in restored["dcs"]:
        item.pop("_shutdown_waiting", None)
    check("restore drops the wait flag", "_shutdown_waiting" not in restored["dcs"][0])
    check(
        "restore clears it in app.py too",
        'dc.pop("_shutdown_waiting", None)' in source,
    )

    # A restart leaves nothing that will submit the save, so the card has to be
    # handed back instead of claiming dCloud is saving.
    check("the claim marks the wait up front", 'dc["_shutdown_waiting"] = True' in source)
    check("submitting the save records it", "saveSubmitted=True" in source)
    check(
        "an interrupted shutdown is handed back",
        "Guest shutdown was interrupted before the save was submitted" in poll,
    )
    check(
        "a card stuck in the saving phase is handed back too",
        'str(dc.get("phase") or "") in {"shutting_down", "saving"}' in poll,
    )
    # A real save in flight must keep its path, so the hand-back only fires after
    # dCloud has been asked whether it is stopping, saving, or already saved.
    check(
        "the hand-back waits for dCloud's answer",
        poll.index("is_saving_in_progress_status(public)") < poll.index("hand_back()\n        return"),
    )
    check("the hand-back reloads the VM list", "_load_dc_vms(job, dc, token)" in poll)


def test_same_demo_can_repeat_in_a_dc() -> None:
    """1.8.4: dCloud issues a new session ID, so a repeat is its own card."""
    import app

    job = {
        "id": "j1",
        "phase": "ready",
        "log": [],
        "dcs": [{"site": "sjc", "demoId": "480730", "sessionId": "1356727", "phase": "ready"}],
    }
    added = app._append_demo_schedule_to_job(job, [("sjc", "480730")], content_export=False)
    live, repeat = job["dcs"]
    check("the repeat is appended, not merged", added == 1 and len(job["dcs"]) == 2)
    check("the repeat starts with no session", not repeat.get("sessionId"))
    check("scheduling targets the pending card", app._find_dc(job, "sjc", demo_id="480730") is repeat)
    check("status still finds the live card", app._find_dc(job, "sjc", "1356727") is live)
    check("only the repeat is queued", [dc for dc in job["dcs"] if app._dc_needs_schedule(dc)] == [repeat])


def test_page_markup_is_balanced() -> None:
    """Hand-editing nested <details>/<div> markup is easy to get wrong, and a
    stray close tag silently swallows half a section in the browser."""
    from html.parser import HTMLParser

    void = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    class Balance(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.stack: list[tuple[str, int]] = []
            self.errors: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag not in void:
                self.stack.append((tag, self.getpos()[0]))

        def handle_endtag(self, tag):
            if tag in void:
                return
            if not self.stack:
                self.errors.append(f"line {self.getpos()[0]}: stray </{tag}>")
                return
            open_tag, line = self.stack[-1]
            if open_tag != tag:
                self.errors.append(
                    f"line {self.getpos()[0]}: </{tag}> closes <{open_tag}> from line {line}"
                )
            self.stack.pop()

    parser = Balance()
    parser.feed(INDEX)
    check("index.html tags are balanced", not parser.errors, "; ".join(parser.errors[:3]))
    check(
        "nothing is left open at the end",
        not parser.stack,
        str([tag for tag, _ in parser.stack[:5]]),
    )


def test_task_groups_collapse() -> None:
    """Mario's ask: the two Hub task sections fold away like the panels do."""
    for group, title in (
        ("cai-task-group", "Content Integration Tasks"),
        ("camgr-task-group", "Content Transfer Tasks"),
    ):
        start = INDEX.index(f'id="{group}"')
        head = INDEX.index("<summary class=\"task-group-head\">", start - 200)
        check(f"{group} is a details section", '<details class="task-group' in INDEX[start - 60 : start])
        check(f"{group} opens by default", 'open>' in INDEX[start : start + 40], INDEX[start : start + 40])
        check(f"{group} has a summary header", head < INDEX.index(title))

    check(
        "collapse-all and the saved layout include them",
        '"details.task-group",' in INDEX,
    )
    check(
        "the caret shows which way the section is folded",
        'details.task-group[open] > summary.task-group-head::before' in INDEX,
    )
    # A button inside <summary> toggles the section unless the handler says no.
    help_toggle = js_function("initHubHelpToggle")
    check(
        "Show help does not collapse the section",
        "ev.preventDefault()" in help_toggle and "ev.stopPropagation()" in help_toggle,
    )
    goto = js_function("goToPageTarget")
    check(
        "an error can still scroll to a button inside a collapsed group",
        'node.tagName === "DETAILS"' in goto and "parentElement" in goto,
    )


def test_ended_card_is_not_a_dead_end() -> None:
    """1.12.7: a card whose dCloud read failed was retired from the unauthenticated
    checkSession answer. It left the page but stayed in the list, so adding the same
    session back was refused as a duplicate and there was no way to recover it.
    """
    import app

    real_last_job = app.LAST_JOB_FILE
    with tempfile.TemporaryDirectory() as tmp:
        app.LAST_JOB_FILE = Path(tmp) / "last-job.json"
        try:
            job = {
                "id": "j-ended",
                "phase": "ready_to_patch",
                "log": [],
                "error": "",
                "dcs": [{"site": "sjc", "sessionId": "492702", "phase": "ready", "status": "Active"}],
            }
            dc = job["dcs"][0]

            real_fetch = app.fetch_session
            real_public = app.check_public_session_status
            real_token = app._refresh_job_token
            try:
                # dCloud answers a stale token with 400 as often as 401, and the
                # unauthenticated endpoint says Deleted for anything it cannot see.
                app.fetch_session = lambda *a, **k: (None, "HTTP 400 Bad Request")
                app.check_public_session_status = lambda *a, **k: ("Deleted", None)
                app._refresh_job_token = lambda *a, **k: ("", "no refresh token")
                app._refresh_dc_from_dcloud(job, dc, "stale-token")
            finally:
                app.fetch_session = real_fetch
                app.check_public_session_status = real_public
                app._refresh_job_token = real_token

            check(
                "a session dCloud could not be asked about keeps its card",
                dc["phase"] == "ready",
                dc["phase"],
            )
            check(
                "the failed lookup is logged instead of being silent",
                any("dCloud lookup failed" in line for line in job["log"]),
                str(job["log"]),
            )

            job["log"] = []
            job["dcs"] = [
                {"site": "sjc", "sessionId": "492702", "phase": "ended", "status": "Deleted"},
                {"site": "rtp", "sessionId": "1358557", "phase": "ended", "monitorOnly": True},
                {"site": "lon", "sessionId": "805992", "phase": "saved", "status": "Saved"},
            ]
            removed = app._retire_ended_cards(job)
            check("ended cards leave the tool entirely", removed == 2, str(removed))
            check(
                "a saved card stays for the saved content list",
                [row["site"] for row in job["dcs"]] == ["lon"],
                str(job["dcs"]),
            )
            check(
                "monitoring removals are logged too",
                any("session monitoring" in line for line in job["log"]),
                str(job["log"]),
            )

            job["dcs"] = [{"site": "sjc", "sessionId": "492702", "phase": "ended"}]
            check(
                "a finished card is cleared so the session can be added back",
                app._drop_ended_card(job, "sjc", "492702") and job["dcs"] == [],
                str(job["dcs"]),
            )
        finally:
            app.LAST_JOB_FILE = real_last_job

    source = (ROOT / "app.py").read_text(encoding="utf-8")
    refresh = source[
        source.index("def _refresh_dc_from_dcloud(") : source.index("def _sync_job_phase(")
    ]
    check(
        "an unreadable session is never retired from the public endpoint",
        "Could not read this session from dCloud" in refresh,
    )
    check("a failed read is retried on a fresh token", refresh.count("_refresh_job_token(") == 1)
    check("every ended card says why in the log", "def retire(" in refresh)
    check(
        "a stopping card is still refreshed",
        "_SKIP_REFRESH_DC_PHASES = _TERMINAL_DC_PHASES" in source
        and '"ending"' in source[
            source.index("_WATCHED_DC_PHASES") : source.index("RESET_GRACE_SECONDS")
        ],
    )

    attach = source[source.index("def _attach_job(") : source.index("def _remove_card(")]
    check("an ended card cannot block an add", '!= "ended"' in attach)
    check("a stale finished card is replaced", "_drop_ended_card(" in attach)

    burn = source[
        source.index("    burn_jobs: dict[int, dict[str, Any]] = {}") : source.index(
            "    primary_burn_job ="
        )
    ]
    check("burn-in joins the job already on screen", "reuse = job" in burn)
    check("a reused job only schedules the new cards", "_schedule_and_watch_new_dcs" in burn)
    check("a reused job is not re-run for its existing cards", "burn_scheduled.get(days)" in burn)


def test_stopping_card_picks_up_a_session_that_started_again() -> None:
    """Refresh used to skip cards in the ending/stopping phase, so a session that
    started again under the same ID kept saying dCloud was tearing it down."""
    import app

    real_last_job = app.LAST_JOB_FILE
    with tempfile.TemporaryDirectory() as tmp:
        app.LAST_JOB_FILE = Path(tmp) / "last-job.json"
        try:
            job = {
                "id": "j-stop",
                "phase": "ending",
                "log": [],
                "error": "",
                "dcs": [{
                    "site": "rtp",
                    "sessionId": "1358790",
                    "phase": "ending",
                    "status": "Stopping",
                    "message": "dCloud is tearing this session down — no save.",
                    "endedWithoutSave": True,
                }],
            }
            dc = job["dcs"][0]
            real_fetch = app.fetch_session
            real_public = app.check_public_session_status
            real_token = app._refresh_job_token
            real_vms = app._load_dc_vms
            try:
                app.check_public_session_status = lambda *a, **k: ("Stopping", None)
                app.fetch_session = lambda *a, **k: (
                    {"status": 2, "sessionStatus": 2, "uid": 1358790},
                    None,
                )
                app._refresh_job_token = lambda *a, **k: ("token", None)
                app._load_dc_vms = lambda *a, **k: None
                app._refresh_dc_from_dcloud(job, dc, "token")
            finally:
                app.fetch_session = real_fetch
                app.check_public_session_status = real_public
                app._refresh_job_token = real_token
                app._load_dc_vms = real_vms

            check("the card leaves the stopping phase", dc["phase"] == "waiting", dc["phase"])
            check(
                "the teardown message is replaced",
                "tearing this session down" not in str(dc.get("message") or ""),
                str(dc.get("message")),
            )
            check(
                "a stale public Stopping does not win over Starting",
                "Starting" in str(dc.get("status") or "") or dc["phase"] == "waiting",
                str(dc.get("status")),
            )
            check(
                "the log says it started again",
                any("no longer stopping" in line for line in job["log"]),
                str(job["log"]),
            )
        finally:
            app.LAST_JOB_FILE = real_last_job


def test_removed_id_can_be_added_back() -> None:
    """Remove from list writes two hide lists. CAI/CAMGR discovery must not
    resurrect the row, but an explicit Add to Hub has to clear both — otherwise
    the row is stored and filtered straight back out and the button looks dead.
    """
    import app
    from app import CaiDemoRef

    real_state = app.MANAGED_SAVED_IDS_FILE
    with tempfile.TemporaryDirectory() as tmp:
        app.MANAGED_SAVED_IDS_FILE = Path(tmp) / "managed-saved-ids.json"
        try:
            job = {"id": "j1", "phase": "ready", "log": [], "dcs": [], "savedIdHidden": []}
            row = {"site": "sjc", "savedId": "483886", "name": "TINY BABY"}

            app._upsert_managed_saved_rows([row])
            check("the row lands in the list", "sjc:483886" not in app._saved_id_hidden_set(job))

            app._hide_saved_ids(job, [CaiDemoRef(site="sjc", saved_id="483886")])
            check("Remove from list hides it", "sjc:483886" in app._saved_id_hidden_set(job))
            check("the job records the hide too", job["savedIdHidden"] == ["sjc:483886"], str(job))

            # A CAMGR sweep re-adds anything in flight. It must not undo a removal,
            # and it must not wipe the managed hide list the way it used to.
            app._upsert_managed_saved_rows([{**row, "name": "TINY BABY - update6"}])
            check(
                "a CAMGR sweep leaves a removed row removed",
                "sjc:483886" in app._managed_hidden_set(),
            )
            check(
                "the swept row is not stored either",
                all(
                    r.get("savedId") != "483886"
                    for r in app._managed_saved_state().get("rows") or []
                ),
            )
            check(
                "the removal survives with no job attached",
                "sjc:483886" in app._saved_id_hidden_set(None),
            )

            # Add to Hub is a deliberate user action, so it clears both lists.
            app._upsert_managed_saved_rows([row], unhide=True)
            app._unhide_saved_ids(job, [row])
            check(
                "adding it back un-hides it everywhere",
                "sjc:483886" not in app._saved_id_hidden_set(job),
                str(job.get("savedIdHidden")),
            )
            check(
                "and the row is back in the list",
                any(
                    r.get("savedId") == "483886"
                    for r in app._managed_saved_state().get("rows") or []
                ),
            )
        finally:
            app.MANAGED_SAVED_IDS_FILE = real_state

    source = (ROOT / "app.py").read_text(encoding="utf-8")
    check("the add endpoint clears the job hide list", "_unhide_saved_ids(job, rows)" in source)
    check(
        "only the explicit add un-hides",
        source.count(", unhide=True)") == 1,
        f"{source.count(', unhide=True)')} callers un-hide",
    )


def test_staggered_session_copies() -> None:
    """Delay + number of sessions: first at the chosen start, later copies wait."""
    from dcloud_client import parse_schedule_datetime, schedule_copy_offsets_minutes

    check("a lone session's delay pushes out its own start", schedule_copy_offsets_minutes(60, 1) == [60])
    check("three sessions stagger by the delay", schedule_copy_offsets_minutes(15, 3) == [0, 15, 30])
    check("copies cap at 20", len(schedule_copy_offsets_minutes(1, 99)) == 20)
    # dCloud delays a same-demo session that starts at the same moment, so five
    # copies with no delay of our own still go out 4 minutes apart.
    check(
        "copies of one demo keep a 4-minute floor",
        schedule_copy_offsets_minutes(0, 5) == [0, 4, 8, 12, 16],
    )
    check(
        "different demos with no delay still share a start",
        schedule_copy_offsets_minutes(0, 1, 3) == [0, 0, 0],
    )
    check(
        "a round of demos counts toward the same-demo gap",
        schedule_copy_offsets_minutes(0, 2, 3) == [0, 0, 0, 4, 4, 4],
    )
    check(
        "a delay wide enough is left alone",
        schedule_copy_offsets_minutes(30, 3) == [0, 30, 60],
    )
    # Mario: three saved items + a 30 minute delay all started at the same time,
    # because only extra copies were staggered.
    check(
        "three different demos stagger too",
        schedule_copy_offsets_minutes(30, 1, 3) == [0, 30, 60],
    )
    check(
        "copies keep their place in the same stagger",
        schedule_copy_offsets_minutes(30, 2, 2) == [0, 30, 60, 90],
    )


def test_extend_offers_the_farthest_bookable_stop() -> None:
    from dcloud_client import (
        _dcloud_timestamp,
        extend_is_capacity_blocked,
        probe_max_extend_stop,
        parse_schedule_datetime,
        resolve_extended_stop_by_minutes,
    )

    booked = "Can't extend session: Resources are fully booked out after the session"
    check("fully booked is treated as capacity", extend_is_capacity_blocked(booked))

    current = datetime(2026, 9, 20, 18, 20, tzinfo=timezone.utc)
    requested = current + timedelta(days=1)
    limit = current + timedelta(hours=4)
    current_s = _dcloud_timestamp(current)
    requested_s = _dcloud_timestamp(requested)
    five, err = resolve_extended_stop_by_minutes(current_stop=current_s, extra_minutes=5 * 24 * 60)
    check("five days is one extend", err is None and parse_schedule_datetime(five) == current + timedelta(days=5))

    calls: list[str] = []

    def put(stop: str) -> dict:
        calls.append(stop)
        when = parse_schedule_datetime(stop)
        if when is None:
            return {"ok": False, "message": "bad stop"}
        if when > limit:
            return {"ok": False, "message": booked}
        return {"ok": True, "stop": stop}

    result = probe_max_extend_stop(
        "token",
        "rtp",
        "1358088",
        requested_stop=requested_s,
        current_stop=current_s,
        put=put,
    )
    check("the full day is not applied", not result.get("applied") and bool(result.get("offer")), str(result))
    got = parse_schedule_datetime(str(result.get("suggested_stop") or ""))
    check(
        "the offer is about 4 hours out",
        got is not None and abs((got - limit).total_seconds()) <= 15 * 60,
        str(result),
    )
    check("the requested stop is tried first", bool(calls) and calls[0] == requested_s)
    check("a successful probe is reverted", current_s in calls)

    full = probe_max_extend_stop(
        "token",
        "rtp",
        "1358088",
        requested_stop=requested_s,
        current_stop=current_s,
        put=lambda stop: {"ok": True, "stop": stop},
    )
    check("a free window is applied", bool(full.get("applied")) and not full.get("offer"), str(full))

    blocked = probe_max_extend_stop(
        "token",
        "rtp",
        "1358088",
        requested_stop=requested_s,
        current_stop=current_s,
        put=lambda stop: {"ok": False, "message": booked},
    )
    check("no shorter window means no offer", not blocked.get("offer") and not blocked.get("applied"), str(blocked))

    check("extend by days is on the page", 'id="extend-days"' in INDEX and 'id="extend-hours"' in INDEX)
    check("the page probes before applying a shorter extend", "/api/jobs/${jobId}/probe-extend" in INDEX)
    check("the page asks before taking the farthest time", "Extend to that time?" in INDEX)
    check("card Extend opens an amount prompt", "extendCardSession(extendBtn)" in INDEX)
    amount_source = js_function("parseExtensionAmount")
    amount_source += """
const cases = [
  ["5", 7200],
  ["5d", 7200],
  ["6h", 360],
  ["5 days 6 hours", 7560],
  ["garbage", 0],
];
for (const [text, expected] of cases) {
  const actual = parseExtensionAmount(text);
  if (actual !== expected) {
    console.error(`${text}: expected ${expected}, got ${actual}`);
    process.exit(1);
  }
}
"""
    run_node(amount_source, "card Extend accepts days and hours")

    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    check("probe-extend is a real endpoint", "/api/jobs/{job_id}/probe-extend" in app_source)

    import app

    recovered = {
        "phase": "error",
        "error": "No sessions reached the ready state.",
        "dcs": [{"phase": "ready"}],
    }
    app._sync_job_phase(recovered)
    check(
        "an active session clears the stale no-ready error",
        recovered["phase"] == "ready_to_patch" and recovered["error"] == "",
        str(recovered),
    )
    check(
        "startup timeout keeps watching instead of becoming an error",
        'job["phase"] = "waiting_active"' in app_source
        and "Sessions are still starting. The cards will keep checking for Active." in app_source,
    )


def test_staggered_session_copies_cards() -> None:
    from dcloud_client import parse_schedule_datetime

    import app
    from app import DemoIds, RunPayload

    payload = RunPayload(
        demo_ids=DemoIds(sjc="480730"),
        content_export=False,
        days=1,
        start_at="2026-09-16T16:00:00Z",
        stop_at="2026-09-17T16:00:00Z",
        delay_minutes=15,
        session_count=3,
    )
    cards = app._expand_schedule_cards([("sjc", "480730")], payload)
    check("three cards from one demo ID", len(cards) == 3)
    starts = [parse_schedule_datetime(card["requestedStart"]) for card in cards]
    check("each copy has a start", all(starts))
    gap = (starts[1] - starts[0]).total_seconds()
    gap2 = (starts[2] - starts[1]).total_seconds()
    check("the gap is 15 minutes", gap == 900 and gap2 == 900, f"{gap}, {gap2}")
    check("all three still need scheduling", all(app._dc_needs_schedule(card) for card in cards))

    # Three saved items checked together are one session each, 30 minutes apart.
    spread = payload.model_copy(update={"delay_minutes": 30, "session_count": 1})
    picked = app._expand_schedule_cards(
        [("rtp", "1391087"), ("rtp", "1391098"), ("rtp", "1391093")], spread
    )
    when = [parse_schedule_datetime(card["requestedStart"]) for card in picked]
    check(
        "different demos are 30 minutes apart",
        [(item - when[0]).total_seconds() for item in when] == [0, 1800, 3600],
        str(when),
    )
    check(
        "only the first card in a DC answers the conflict prompt",
        [bool(card.get("scheduleDecisionCard")) for card in picked] == [True, False, False],
    )
    lone = app._expand_schedule_cards(
        [("sjc", "480730")], payload.model_copy(update={"delay_minutes": 30, "session_count": 1})
    )
    check("a single session still waits out the delay", lone[0]["scheduleOffsetMinutes"] == 30)

    now = datetime(2026, 9, 19, 17, 30, tzinfo=timezone.utc)
    stale = now - timedelta(minutes=20)
    mario = payload.model_copy(
        update={
            "demo_ids": DemoIds(rtp="1349630"),
            "start_at": stale.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "stop_at": (stale + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "delay_minutes": 5,
            "session_count": 1,
            "days": 1,
        }
    )
    waited = app._expand_schedule_cards([("rtp", "1349630")], mario, now=now)
    got = parse_schedule_datetime(waited[0]["requestedStart"])
    check(
        "a stale start plus a 5 minute delay is now plus 5 minutes",
        got == now + timedelta(minutes=5),
        str(got),
    )
    later = now + timedelta(hours=2)
    future = mario.model_copy(
        update={
            "start_at": later.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "stop_at": (later + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )
    kept = app._expand_schedule_cards([("rtp", "1349630")], future, now=now)
    check(
        "a future start is left alone",
        parse_schedule_datetime(kept[0]["requestedStart"]) == later + timedelta(minutes=5),
        str(kept[0].get("requestedStart")),
    )
    check("the page refreshes a stale start before scheduling", "bumpScheduleStartIfPast" in INDEX)
    check(
        "the page sends the start unshifted now that the server staggers",
        "const firstDelay" not in INDEX and 'shiftedScheduleIso("sched-start", 0)' in INDEX,
    )
    copies = payload.model_copy(update={"delay_minutes": 0, "session_count": 5})
    spaced = app._expand_schedule_cards([("sjc", "480730")], copies)
    check(
        "five copies with no delay are 4 minutes apart",
        [card["scheduleOffsetMinutes"] for card in spaced] == [0, 4, 8, 12, 16],
    )
    check(
        "the page explains the same-demo floor",
        "MIN_SAME_DEMO_GAP_MINUTES = 4" in INDEX,
    )

    days = INDEX.find('id="days"')
    delay = INDEX.find('id="sched-delay"')
    copies = INDEX.find('id="sched-copies"')
    start = INDEX.find('id="sched-start"')
    check("delay and copies sit next to duration", -1 < days < delay < copies < start)
    check(
        "saved-content management is its own card",
        'id="panel-saved-schedule"' in INDEX
        and "Manage your own saved content across all DCs" in INDEX,
    )
    check(
        "cleanup no longer holds the saved-content scheduler",
        "Start new sessions from saved content" not in INDEX,
    )
    check(
        "saved-content find and delete live in the management card",
        'id="btn-find-schedule-saved"' in INDEX
        and 'id="btn-delete-schedule-saved"' in INDEX
        and 'id="btn-find-saved"' not in INDEX,
    )
    check(
        "cleanup is surveys only",
        "Session feedback surveys" in INDEX
        and 'id="btn-decline-all-surveys"' in INDEX
        and 'id="found-saved-panel"' not in INDEX,
    )
    check(
        "the management list keeps a collapsible group per DC",
        "function renderManagedSavedTable(site, rows" in INDEX
        and 'groupRowsBySite(deletableRows).filter((group) => group.rows.length > 0)' in INDEX
        and 'id="btn-expand-schedule-saved"' in INDEX
        and 'id="btn-collapse-schedule-saved"' in INDEX
        and "#found-schedule-saved details.dc-group" in INDEX,
    )
    check(
        "one column pick sorts every DC table, with Name first and Saved last",
        all(
            f'sortTh("{key}"' in INDEX
            for key in ("savedAt", "name", "contentId", "owner", "state")
        )
        and INDEX.index('${sortTh("name", "Name")}\n              ${sortTh("contentId"')
        < INDEX.index('${sortTh("state", "State")}\n              ${sortTh("savedAt", "Saved")}'),
    )
    check(
        "saved-content rows have the requested actions",
        all(
            marker in INDEX
            for marker in (
                "btn-managed-schedule",
                "Edit topology",
                "btn-saved-share",
                "btn-managed-delete",
            )
        ),
    )
    check(
        "bulk delete confirms the exact cross-DC selection",
        'id="btn-delete-schedule-saved"' in INDEX
        and "Permanently delete ${selected.length} saved content item(s)?" in INDEX
        and "${row.site.toUpperCase()} ${row.content_id} — ${row.name}" in INDEX,
    )
    check(
        "TBv2 promoted content stays EOL-only and cannot render Delete",
        "Topology Builder v2 promoted — EOL only" in INDEX
        and "renderManagedSavedTable(site, siteRows, { deletable: false" in INDEX
        and 'deletable ? `<button type="button" class="danger btn-managed-delete"' in INDEX,
    )
    check(
        "Search dCloud no longer offers Share",
        'data-action="session-share"' not in INDEX,
    )
    check(
        "the saved-content scheduler has the nearby-slot checkbox",
        "saved-auto-next-available" in INDEX
        and INDEX.count("Use a nearby slot when resources are busy") >= 2,
    )


def test_schedule_capacity_explains_and_stays_nearby() -> None:
    import dcloud_client

    reason = (
        "Can't schedule an instance of demo 1376509: The following resources "
        "are unavailable: Resource 'Device(PSTN Services)' requires 1 but only has 0 available"
    )
    check(
        "the blocked resource is named",
        dcloud_client.unavailable_resource_name(reason) == "Device(PSTN Services)",
    )

    begin = datetime(2026, 9, 16, 19, 0, tzinfo=timezone.utc)
    real_calendar = dcloud_client.fetch_content_calendar
    real_pools = dcloud_client._pool_attempts_for_demo
    try:
        dcloud_client.fetch_content_calendar = lambda *args, **kwargs: ([], None)
        shorter = dcloud_client.find_shorter_schedule_option(
            "token",
            "rtp",
            "1376509",
            desired_start=begin,
            requested_duration=timedelta(days=100),
            pool_id="GDE_CONTENT_DEV",
        )
        check("a 100-day request can suggest 30 days", shorter == (begin, begin + timedelta(days=30), 30))

        # Capacity remains blocked beyond tomorrow. Even though a one-day slot
        # exists later, it is too far away to offer or schedule automatically.
        block = {
            "type": "UNAVAILABLE",
            "start": begin.isoformat(),
            "stop": (begin + timedelta(days=3)).isoformat(),
        }
        dcloud_client.fetch_content_calendar = lambda *args, **kwargs: ([block], None)
        no_soon_option = dcloud_client.find_shorter_schedule_option(
            "token",
            "rtp",
            "1376509",
            desired_start=begin,
            requested_duration=timedelta(days=100),
            pool_id="GDE_CONTENT_DEV",
        )
        check("a slot after tomorrow is not suggested", no_soon_option is None)

        dcloud_client._pool_attempts_for_demo = lambda *args, **kwargs: [
            ("GDE_CONTENT_DEV", "GDE_CONTENT_DEV")
        ]
        conflict = dcloud_client.find_schedule_conflict(
            "token",
            "rtp",
            "1376509",
            days=1,
            start_at=begin.isoformat(),
            stop_at=(begin + timedelta(days=1)).isoformat(),
        )
        check("calendar reports the conflict", bool(conflict and conflict.get("conflict")))
        check("a next slot over 24 hours away is suppressed", not conflict.get("nextStart"), str(conflict))
    finally:
        dcloud_client.fetch_content_calendar = real_calendar
        dcloud_client._pool_attempts_for_demo = real_pools

    check(
        "the page says next means within 24 hours",
        "only if it starts within the next 24 hours" in INDEX,
    )
    check("failed schedule cards have an adjust button", "btn-adjust-schedule" in INDEX)
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    check(
        "the failed card keeps the resource and date details",
        'scheduleConflict=availability' in app_source
        and '"resource",' in app_source
        and '"suggestedDays",' in app_source,
    )
    check(
        "the card displays dCloud's technical reason",
        "Technical reason from dCloud" in INDEX,
    )
    adjust = js_function("adjustScheduleFromCard")
    for field in ("days", "sched-delay", "sched-copies"):
        check(f"Adjust schedule fills {field}", field in adjust)
    check(
        "Adjust schedule fills both date controls",
        'applyDateToControls("start"' in adjust and 'applyDateToControls("stop"' in adjust,
    )


def test_schedule_does_not_require_load_vms() -> None:
    """1.8.5: a regular session powers its own VMs, so Load VMs is not needed."""
    import threading

    import app
    from app import HTTPException, ScheduleSavedItem, ScheduleSavedPayload

    items = [ScheduleSavedItem(site="sjc", content_id="480730", name="demo")]

    exported = ScheduleSavedPayload(items=items, selected_vms=[], content_export=True)
    try:
        app._schedule_saved_job(exported)
    except HTTPException as err:
        check("exported with no VMs still asks for picks", "Load VMs" in str(err.detail), str(err.detail))
    else:
        check("exported with no VMs still asks for picks", False, "no guard fired")

    real_thread, real_token, real_job_file = threading.Thread, app._resolve_token, app.LAST_JOB_FILE
    started: list[str] = []

    class StubThread:
        def __init__(self, target=None, args=(), daemon=None):
            started.append(getattr(target, "__name__", "?"))

        def start(self) -> None:
            pass

    with tempfile.TemporaryDirectory() as tmp:
        threading.Thread = StubThread
        app._resolve_token = lambda *a, **k: "stub-token"
        app.LAST_JOB_FILE = Path(tmp) / "last-job.json"
        try:
            regular = ScheduleSavedPayload(
                items=items, selected_vms=[], content_export=False, skip_power_on=True
            )
            job = app._schedule_saved_job(regular)
            check("regular schedule runs with nothing checked", len(job["dcs"]) == 1)
            check("it really starts the scheduler", started == ["_run_job"], str(started))
        finally:
            threading.Thread, app._resolve_token, app.LAST_JOB_FILE = real_thread, real_token, real_job_file


def test_pending_schedule_payload() -> None:
    from app import ScheduleSavedPayload

    body = ScheduleSavedPayload(skip_power_on=True)
    check("saved-content payload carries skip_power_on", body.skip_power_on is True)


# --------------------------------------------------------------------------
# Page rules (1.8.1, 1.8.2, 1.8.3, and the Load VMs placement)
# --------------------------------------------------------------------------

def test_refresh_starts_clean() -> None:
    """1.8.1 / 1.8.2: a page-load restore brings back cards, not input values."""
    check(
        "bootstrap restore does not refill inputs",
        "applyJobToForm(job, { fillInputs: false })" in INDEX,
    )
    check(
        "auto-restore does not refill inputs",
        "applyJobToForm(job, { fillInputs: !auto })" in INDEX,
    )
    check(
        "user-driven attach still refills",
        INDEX.count("applyJobToForm(job);") >= 1,
    )
    cleared = js_function("clearRestoredIdFields")
    for field in ("src-session", "hub-manual-id", "cai-template-demo-id", "skip-catalog"):
        check(f"refresh clears {field}", field in cleared)


def test_demo_id_fields_can_be_cleared() -> None:
    """Catalog lookup fills every DC; those IDs need a one-click undo."""
    check("each DC has a Clear control", INDEX.count('class="clear-demo-id"') == 5)
    check("Clear all IDs is on the schedule form", 'id="btn-clear-all-demo-ids"' in INDEX)
    check("Clear all reuses the existing clearer", "clearDemoIdFields()" in INDEX)
    check("one box can be cleared without the rest", "function clearOneDemoId(" in INDEX)


def test_cancel_wording_for_scheduled_cards() -> None:
    """1.8.3: End is for live work; a card that has not started says Cancel."""
    cases = [
        ({"sessionId": "1", "phase": "waiting", "status": "Scheduled"}, True, "scheduled"),
        ({"sessionId": "1", "phase": "waiting", "status": "1 / SCHEDULED"}, True, "numeric scheduled"),
        ({"sessionId": "1", "phase": "waiting", "status": ""}, True, "waiting, no status yet"),
        ({"sessionId": "1", "phase": "waiting", "status": "2 / STARTING_UP"}, False, "starting up"),
        ({"sessionId": "1", "phase": "ready", "status": "Active"}, False, "active"),
        ({"sessionId": "1", "phase": "saving", "status": "12 / SAVING"}, False, "saving"),
        ({"sessionId": "", "phase": "waiting", "status": "Scheduled"}, False, "no session id"),
    ]
    source = "\n".join(
        js_function(name) for name in ("phaseLabel", "cardStatusLabel", "isScheduledNotStarted")
    )
    source += f"""
const cases = {json.dumps(cases)};
for (const [dc, want, why] of cases) {{
  const got = isScheduledNotStarted(dc);
  if (got !== want) {{
    console.error(`${{why}}: expected ${{want ? "Cancel" : "End"}} session, got ${{got ? "Cancel" : "End"}}`);
    process.exit(1);
  }}
}}
"""
    run_node(source, "scheduled cards say Cancel, live ones say End")
    check("card button follows the status", 'data-tip="${endTip}">${endLabel}' in INDEX)


def test_load_vms_button_stays_on_top() -> None:
    """Mario's ask: Load VMs sits with Connect, above the VM list, and says
    which demo it will pull."""
    for group, button, note in (
        ("cai", "btn-cai-load-vms", "cai-load-target"),
        ("camgr", "btn-camgr-load-vms", "camgr-load-target"),
    ):
        connect = INDEX.index(f'id="btn-{group}-connect"')
        load = INDEX.index(f'id="{button}"')
        vm_list = INDEX.index(f'id="{group}-vm-list"')
        check(f"{group}: Load button sits above the VM list", connect < load < vm_list)
        check(f"{group}: the note sits next to the button", load < INDEX.index(f'id="{note}"') < vm_list)

    rows_cases = [
        ([], "VMs", "Check a row"),
        ([{"site": "sjc", "saved_id": "483886", "name": "TINY BABY"}], "VMs", "SJC 483886 — TINY BABY"),
        (
            [
                {"site": "sjc", "saved_id": "483886", "name": "TINY BABY"},
                {"site": "rtp", "saved_id": "483999", "name": "TINY BABY"},
            ],
            "transfer VMs",
            "same demo in RTP",
        ),
        (
            [
                {"site": "sjc", "saved_id": "1", "name": "One demo"},
                {"site": "rtp", "saved_id": "2", "name": "Another demo"},
            ],
            "VMs",
            "Two different demos",
        ),
        ([{"site": "CDEV.RTP", "saved_id": "9", "name": "dev"}], "transfer VMs", "ContentDEV"),
    ]
    source = "\n".join(js_function(name) for name in ("demoGroupKey", "camgrCdevHomeDc", "loadTargetNote"))
    source += f"""
const cases = {json.dumps(rows_cases)};
for (const [rows, what, expected] of cases) {{
  const note = loadTargetNote(rows, what);
  if (!note.text.includes(expected)) {{
    console.error(`expected note to mention "${{expected}}", got "${{note.text}}"`);
    process.exit(1);
  }}
}}
const mixed = loadTargetNote(cases[3][0], "VMs");
if (!mixed.warn) {{ console.error("mixed demos should warn"); process.exit(1); }}
const single = loadTargetNote(cases[1][0], "VMs");
if (single.warn) {{ console.error("a single demo should not warn"); process.exit(1); }}
// The "change it" sentence goes on its own line, so it must not be glued
// onto the end of the demo name where it word-wraps badly.
if (single.text.includes("Check a different row")) {{
  console.error(`the change-row sentence belongs in note.hint, not note.text: "${{single.text}}"`);
  process.exit(1);
}}
if (!String(single.hint || "").includes("Check a different row")) {{
  console.error(`expected a hint line, got "${{single.hint}}"`);
  process.exit(1);
}}
if (!single.text.trimEnd().endsWith(".")) {{
  console.error(`the first line should end cleanly, got "${{single.text}}"`);
  process.exit(1);
}}
"""
    run_node(source, "Load VMs note names the demo it will pull")
    check(
        "the hint line renders as its own block",
        ".load-target .load-target-hint" in INDEX and "load-target-hint" in INDEX,
    )

    hint_src = js_function("scheduleCopyHintText")
    hint_src += """
const MIN_SAME_DEMO_GAP_MINUTES = 4;
const cases = [
  [0, 1, ""],
  [60, 1, "one every 60 minutes"],
  [15, 3, "one every 15 minutes"],
  [0, 3, "4 minutes apart"],
];
for (const [delay, copies, expected] of cases) {
  const text = scheduleCopyHintText(delay, copies);
  if (expected && !text.includes(expected)) {
    console.error(`hint(${delay}, ${copies}) missing "${expected}": ${text}`);
    process.exit(1);
  }
  if (!expected && text) {
    console.error(`hint(0, 1) should be empty, got ${text}`);
    process.exit(1);
  }
}
"""
    run_node(hint_src, "delay/copies hint matches the schedule math")


def test_event_management_section() -> None:
    import dcloud_client

    source = (ROOT / "app.py").read_text(encoding="utf-8")
    check(
        "admin status 95 and 99 have their dCloud labels",
        dcloud_client.format_status(95) == "VC Unavailable"
        and dcloud_client.format_status(99) == "Error",
    )
    check(
        "VC Unavailable is read-only and Error only adds Reset to the read actions",
        'const unavailable = String(row.rawStatus ?? "") === "95"' in INDEX
        and 'const error = String(row.rawStatus ?? "") === "99"' in INDEX
        and "const attachable = !terminal && !unavailable && !error" in INDEX
        and 'active || (error && row.canReset !== false)' in INDEX,
    )
    check(
        "event actions poll until Reset or End reaches its settled state",
        "const eventActionWatches = new Map()" in INDEX
        and "function eventWatchIsDone(watch, entry)" in INDEX
        and "watch.activePolls >= 2" in INDEX
        and "20 * 60 * 1000" in INDEX
        and "pollEventActionWatches()" in INDEX
        and "10 * 1000" in INDEX,
    )
    check(
        "Events is a separate top-level section with site and event ID inputs",
        'id="panel-events"' in INDEX
        and 'id="event-site"' in INDEX
        and 'id="event-id"' in INDEX
        and 'id="btn-event-add"' in INDEX,
    )
    check(
        "multiple events persist and render inside site and event groups",
        "dcloud-content-manager-events-v1" in INDEX
        and 'class="dc-group event-site-group"' in INDEX
        and 'class="event-group"' in INDEX
        and "persistEventRefs()" in INDEX,
    )
    check(
        "event sessions support check all, uncheck all, reset, and end",
        all(
            marker in INDEX
            for marker in (
                "event-check-all",
                "event-uncheck-all",
                "event-reset-checked",
                "event-end-checked",
                "event-session-reset",
                "event-session-end",
            )
        ),
    )
    check(
        "event endpoints validate lookup and limit bulk changes",
        '@app.post("/api/events/lookup")' in source
        and '@app.post("/api/events/session-action")' in source
        and "len(session_ids) > 250" in source
        and 'action not in {"end", "reset"}' in source,
    )
    check(
        "bulk event actions are paced one at a time instead of fired in parallel",
        "delay_seconds: float = Field(default=1.0, ge=0, le=30)" in source
        and "time.sleep(body.delay_seconds)" in source
        and "ThreadPoolExecutor" not in source[source.index('@app.post("/api/events/session-action")'):]
        .split("@app.post", 2)[1],
    )
    check(
        "a failed bulk action reports dCloud's reason instead of only a count",
        '"failures": failures' in source
        and "result.failures || []" in INDEX
        and "First reason — ${failures[0]}" in INDEX
        and 'id="event-action-delay"' in INDEX,
    )

    check(
        "a session you do not own falls back to the admin route for reset and end",
        "def _session_action(" in (ROOT / "dcloud_client.py").read_text(encoding="utf-8")
        and 'f"/api/admin/sessions/{sid}/{action}"' in (ROOT / "dcloud_client.py").read_text(encoding="utf-8")
        and "_looks_like_permission_error" in (ROOT / "dcloud_client.py").read_text(encoding="utf-8"),
    )

    client_source = (ROOT / "dcloud_client.py").read_text(encoding="utf-8")
    real_request = dcloud_client._request

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    try:
        calls: list[str] = []

        def fake_request(method, url, token, **kwargs):
            calls.append(url)
            if "/api/admin/sessions/" in url:
                return FakeResponse(200, {"success": True, "message": []})
            return FakeResponse(400, {
                "message": "The content you are trying to access has either been "
                           "removed or you do not have the permission required to view it."
            })

        dcloud_client._request = fake_request
        result = dcloud_client.reset_session("token", "sjc", "491872")
        check(
            "the admin route is used after a permission error",
            result["ok"] is True
            and calls == [
                "https://dcloud2-sjc.cisco.com/api/sessions/491872/reset",
                "https://dcloud2-sjc.cisco.com/api/admin/sessions/491872/reset",
            ],
            str(calls),
        )

        calls.clear()
        end_result = dcloud_client.end_session("token", "sjc", "491872")
        check(
            "End falls back the same way",
            end_result["ok"] is True and calls[-1].endswith("/api/admin/sessions/491872/end"),
            str(calls),
        )

        calls.clear()

        def owner_ok(method, url, token, **kwargs):
            calls.append(url)
            return FakeResponse(200, {"success": True, "message": []})

        dcloud_client._request = owner_ok
        dcloud_client.reset_session("token", "sjc", "123")
        check(
            "a session you own still uses the plain route only",
            calls == ["https://dcloud2-sjc.cisco.com/api/sessions/123/reset"],
            str(calls),
        )
    finally:
        dcloud_client._request = real_request

    real_fetch = dcloud_client.fetch_admin_records
    try:
        def fake_fetch(token, site, *, resource, refresh=False):
            if resource == "events":
                return [{
                    "uid": 399485,
                    "name": "Workshop",
                    "status": "active",
                    "approval": "direct",
                    "sessionCount": 1,
                    "eventStart": "2026-09-19T23:00:00Z",
                    "eventEnd": "2026-09-24T21:00:00Z",
                }], None
            return [{
                "uid": 491872,
                "event": {"uid": 399485, "student": "student1"},
                "name": "Lab session",
                "owner": "owner1",
                "parentId": 483939,
                "virtualCenter": 10,
                "status": 4,
                "canReset": True,
                "start": "2026-09-19T23:00:00Z",
                "stop": "2026-09-24T21:01:00Z",
            }, {
                "uid": 999999,
                "event": {"uid": 123},
                "status": 4,
            }], None

        dcloud_client.fetch_admin_records = fake_fetch
        event, error = dcloud_client.list_event_sessions("token", "sjc", "399485")
        check("event lookup filters sessions by event ID", error is None and len(event["sessions"]) == 1)
        row = event["sessions"][0]
        check(
            "event session rows expose action and display fields",
            row["sessionId"] == "491872"
            and row["status"] == "Active"
            and row["canReset"] is True
            and row["demoId"] == "483939"
            and row["virtualCenter"] == "10",
            str(row),
        )
        fake_status = {
            "uid": 491873,
            "event": {"uid": 399485},
            "name": "Broken session",
            "parentId": 483939,
            "status": 95,
            "canReset": False,
        }
        def status_fetch(token, site, *, resource, refresh=False):
            if resource == "events":
                return [{"uid": 399485, "name": "Workshop", "sessionCount": 1}], None
            return [fake_status], None
        dcloud_client.fetch_admin_records = status_fetch
        unavailable, error = dcloud_client.list_event_sessions("token", "sjc", "399485")
        check(
            "event session preserves raw 95 while showing VC Unavailable",
            error is None
            and unavailable["sessions"][0]["rawStatus"] == 95
            and unavailable["sessions"][0]["status"] == "VC Unavailable",
        )
    finally:
        dcloud_client.fetch_admin_records = real_fetch


def test_tool_owned_browser_avoids_keychain() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    start = (ROOT / "start.command").read_text(encoding="utf-8")
    reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    check("Playwright is a runtime dependency", "playwright>=" in reqs)
    check(
        "start.command downloads Chromium once into this install",
        "playwright install chromium" in start
        and "PLAYWRIGHT_BROWSERS_PATH" in start,
    )
    check(
        "the tool browser module is packaged",
        (ROOT / "tool_browser.py").is_file()
        and "tool_browser.py" in (ROOT / "pack_for_mac.py").read_text(encoding="utf-8"),
    )
    camgr_auto = source[source.index("def _camgr_auto_connect(") : source.index("def _cai_auto_connect(")]
    cai_auto = source[source.index("def _cai_auto_connect(") : source.index("def _auth_keepalive_loop(")]
    check(
        "background CAMGR refresh does not decrypt Chrome cookies",
        "import_camgr_cookies_from_chrome" not in camgr_auto
        and "capture_camgr_session(headed=False)" in camgr_auto,
    )
    check(
        "background CAI refresh does not decrypt Chrome cookies",
        "import_cai_cookies_from_chrome" not in cai_auto
        and "capture_cai_session(headed=False)" in cai_auto,
    )
    check(
        "status polling does not scan Chrome for a refresh token",
        "_maybe_backfill_refresh_from_chrome()" not in source[source.index("def api_auth_status(") : source.index("def api_login_url(")],
    )
    login = source[
        source.index("async def api_dcloud_browser_login(") : source.index(
            '@app.post("/api/dcloud/token/validate")'
        )
    ]
    check(
        "dCloud sign-in goes through the tool browser, not Chrome",
        "capture_dcloud_tokens" in login
        and "try_import_dcloud_session" not in login,
    )
    check(
        "a silent warm-up can never open a sign-in window",
        "allow_window" in login
        and login.index("allow_window") < login.index("headed=True"),
    )
    check(
        "nothing decrypts Chrome cookies for a refresh token any more",
        "_maybe_backfill_refresh_from_chrome" not in source
        and "scan_dcloud_refresh_from_chrome" not in source,
    )
    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    check(
        "the Import from browser buttons are gone",
        'id="btn-import"' not in page and 'id="token-alert-import"' not in page,
    )
    check(
        "one Log in button drives dCloud sign-in through the tool browser",
        'id="btn-login"' in page
        and "/api/auth/dcloud-browser-login" in page
        and "import-from-browser" not in page,
    )
    check(
        "page load shows the saved session without starting a browser",
        "warmAuthFromToolBrowser" in page
        and "loginToDcloud" not in page[page.index("async function warmAuthFromToolBrowser") : page.index("async function refreshAuth")],
    )
    browser = (ROOT / "tool_browser.py").read_text(encoding="utf-8")
    check(
        "dCloud sign-in starts at the SSO authorize URL, not the marketing home",
        "build_dcloud_login_url" in browser
        and 'f"https://dcloud2-{site_code}.cisco.com/"' not in browser,
    )
    check(
        "a silent capture gives up once it lands on a login page",
        "_is_idp_page" in browser
        and "IDP_SETTLE_SECONDS" in browser
        and browser.count("not headed\n") >= 2,
    )
    check(
        "the auth code is caught on navigation, not only by sampling the URL",
        "framenavigated" in browser and "_code_from_url" in browser,
    )
    check(
        "the old popup polling loop is gone",
        not any(
            name in page
            for name in ("pollLoginStorage", "pollLoginImport", "snapshotChromeToken", "loginPopup")
        ),
    )
    check(
        "Connect to CAMGR prefers the tool browser",
        "capture_camgr_session()" in source[source.index("def _connect_camgr(") : source.index("def _auth_camgr_needed(")],
    )


def test_compact_reorderable_session_cards_and_save_description() -> None:
    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    client = (ROOT / "dcloud_client.py").read_text(encoding="utf-8")
    check(
        "save description is multiline and states dCloud's 255-character limit",
        '<textarea id="prompt-alert-description" maxlength="255"' in page
        and 'id="prompt-alert-description-count"' in page
        and "dCloud saved-content descriptions are limited to 255 characters" in page,
    )
    check(
        "save description character count updates while typing",
        "function updateSaveDescriptionCount()" in page
        and '"prompt-alert-description")?.addEventListener("input", updateSaveDescriptionCount)' in page,
    )
    check(
        "the dCloud save payload enforces the same 255-character limit",
        '"description": desc[:255]' in client
        and "tbv3 requires description length 1–255" in client,
    )
    check(
        "workspace and monitoring cards are grouped into collapsible site sections",
        "function renderCardGroups(" in page
        and 'class="card-site-group"' in page
        and 'class="card-site-count"' in page
        and "renderCardGroups(\n            visibleJob" in page
        and "renderCardGroups(\n            visibleMonitor" in page,
    )
    check(
        "session cards collapse to a compact status row",
        '<details class="card${monitor ? " card-monitor" : ""}' in page
        and 'class="card-summary-name"' in page
        and 'class="card-open-session"' in page
        and 'class="card-summary-end"' not in page
        and 'dc.sessionId ? `#${dc.sessionId}`' not in page
        and 'dc.sessionId ? String(dc.sessionId)' in page,
    )
    check(
        "card Actions is on the collapsed row, not buried in the expanded body",
        'summary data-tip="Save, extend, end, or move this card.">Actions</summary>' in page
        and "${openSession}\n                ${actionsMenu}" in page
        and "card-actions-row" not in page
        and "Card actions</summary>" not in page
        and "summary .card-actions-menu" in page
        and 'if (menu.classList.contains("card-actions-menu")) positionSearchActionsMenu(menu);' in page,
    )
    check(
        "session cards can be expanded or collapsed together",
        'id="btn-expand-job-cards"' in page
        and 'id="btn-collapse-job-cards"' in page
        and 'id="btn-expand-monitor-cards"' in page
        and 'id="btn-collapse-monitor-cards"' in page
        and "function setSessionCardsOpen(" in page,
    )
    check(
        "job workspace matches monitoring with site expand/collapse and a cards label",
        'id="job-cards-label"' in page
        and 'id="btn-expand-job-dcs"' in page
        and 'id="btn-collapse-job-dcs"' in page
        and 'id="btn-expand-monitor-dcs"' in page
        and 'id="btn-card-check-all"' in page[page.index('id="job-card-view-actions"') : page.index('id="cards"')]
        and "function setCardSiteGroupsOpen(" in page,
    )
    check(
        "card drag order persists by workspace or monitoring site",
        'class="card-drag-handle"' in page
        and "CARD_LAYOUT_KEY" in page
        and "captureCardLayoutFromDom" in page
        and 'grid.addEventListener("dragstart"' in page
        and 'grid.addEventListener("drop"' in page,
    )


def test_go_to_demo_lands_on_content_not_the_v2_builder() -> None:
    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    check(
        "no session or event row links to the v2 /demo/{id} page",
        ".cisco.com/demo/${encodeURIComponent(row.demoId)}" not in page,
    )
    check(
        "Go to demo shows the parent content inside the tool",
        "async function showContentForDemo(" in page
        and 'data-action="session-content"' in page
        and 'class="event-session-content"' in page
        and 'if (action === "session-content")' in page
        and 'ev.target.closest(".event-session-content")' in page,
    )
    check(
        "the content jump loads that datacenter's Content list when it is missing",
        'await loadUnifiedDcData(["content"], { sites: [dc] })' in page
        and "async function loadUnifiedDcData(sources, { refreshData = false, sites: only = null } = {})" in page
        and 'unifiedSectionOpen.set("content", true)' in page,
    )


def test_repeated_content_states_are_shown_once() -> None:
    import app
    import dcloud_client

    # dCloud sends "saved, promoted, shared, promoted" on some shared content.
    repeated = ["saved", "promoted", "shared", "promoted"]
    check(
        "a repeated state is dropped, keeping dCloud's order",
        dcloud_client.unique_states(repeated) == ["saved", "promoted", "shared"],
    )
    check(
        "saved content rows show each state once",
        dcloud_client.summarize_saved_content(
            {"state": list(repeated), "name": "x", "uid": 1}, "rtp"
        )["state"] == "saved, promoted, shared",
    )
    check(
        "Search dCloud content rows show each state once",
        app._unified_content_result("rtp", {"state": list(repeated), "demoId": "1"})["status"]
        == "saved / promoted / shared",
    )
    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    check(
        "the State column no longer carries a TBv3 badge",
        "TBv3</span>" not in page and "isTbv3" not in page,
    )


def test_cross_dc_lists_have_the_same_instant_filter() -> None:
    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    for input_id in (
        "filter-schedule-saved",
        "filter-saved-ids-found",
        "filter-workspace-sessions",
        "filter-found-sessions",
        "filter-events",
    ):
        check(
            f"{input_id} filters its cross-DC list and reports what it is showing",
            f'id="{input_id}"' in page and f'id="{input_id}-status"' in page,
        )
    check(
        "one shared filter drives every list",
        "const LIST_FILTERS = [" in page
        and "function applyListFilter(config)" in page
        and "function refreshListFilters()" in page
        and '$(config.input)?.addEventListener("input", () => applyListFilter(config))' in page,
    )
    check(
        "rendering a list reapplies the current filter",
        'applyListFilter(listFilterConfig("filter-found-sessions"))' in page
        and 'applyListFilter(listFilterConfig("filter-workspace-sessions"))' in page
        and 'applyListFilter(listFilterConfig("filter-events"))' in page
        and page.count("refreshListFilters();") >= 3,
    )
    check(
        "Check all takes only the rows the filter is showing",
        "function setBoxesChecked(boxes, checked)" in page
        and "if (checked && rowIsFilteredOut(box)) return;" in page
        and "setBoxesChecked(document.querySelectorAll(itemSelector), checked)" in page
        and 'setBoxesChecked(document.querySelectorAll(".found-session:not(:disabled)"), checked)' in page
        and 'setBoxesChecked(document.querySelectorAll(".workspace-found-session:not(:disabled)"), checked)' in page
        and 'setBoxesChecked(group.querySelectorAll(".event-session-check:not(:disabled)"), true)' in page,
    )
    check(
        "a filter change keeps earlier selections and says how many are hidden",
        "let hiddenChecked = 0;" in page
        and "checked rows are hidden by this filter" in page
        # Hiding a row must never silently clear it.
        and "box.checked = false;\n        });\n      });" not in page,
    )
    check(
        "finding saved content no longer pre-checks Content Automation Hub rows",
        # Only the Recheck saved IDs button may still call it.
        page.count("= markSavedIdsOnFoundContent();") == 1
        and "auto-checked ${marked} from Saved content IDs" not in page
        and 'id="btn-recheck-schedule-saved-ids"' in page
        and "function recheckSavedIdsOnFoundContent(" in page,
    )
    check(
        "Hub saved-content rows are not auto-checked on refresh",
        "const defaultAll = existingBoxes.length === 0;" not in page
        and 'const isChecked = checked.has(key) ? " checked" : "";' in page,
    )
    check(
        "top chrome buttons are smaller than primary actions",
        ".header-actions button {" in page
        and "min-width: 10rem;" in page
        and "min-width: 12.5rem;" not in page
        and ".layout-toolbar button {" in page
        and "padding: 0.28rem 0.65rem;" in page,
    )
    check(
        "Load VMs sits inside Schedule sessions, not as its own section",
        page.index('id="panel-step3"') < page.index('id="panel-step2"')
        and page.index('id="panel-step2"') < page.index('id="schedule-dc-fields"')
        and '"panel-step2",' not in page
        and "function revealScheduleLoadVms(" in page
        and 'class="advanced schedule-load-vms"' in page,
    )
    check(
        "list columns can be dragged wider and remember it",
        "function makeColumnsResizable(containerId)" in page
        and "function startColumnResize(ev, containerId, table, index)" in page
        and 'COLUMN_WIDTH_KEY = "dcloud-tool-column-widths-v3"' in page
        and 'grip.className = "col-resizer"' in page
        and ".col-resizer {" in page
        and "makeColumnsResizable(config.container);" in page
        and 'makeColumnsResizable("unified-search-results");' in page,
    )
    check(
        "a resize drag neither sorts the column nor leaves names capped",
        'grip.addEventListener("click", (click) => {' in page
        and "click.stopPropagation();" in page
        and 'grip.addEventListener("dblclick"' in page
        and "table.cols-resized td.name-cell { max-width: none; }" in page
        and "overflow-wrap: anywhere;" in page
        and "word-break: break-word;" in page,
    )
    check(
        "a widened table scrolls inside its panel instead of overflowing it",
        '.table-scroll { overflow-x: auto; max-width: 100%; }' in page
        and 'scroller.className = "table-scroll"' in page
        and 'table.parentElement?.classList.contains("table-scroll")' in page,
    )
    check(
        "the last column keeps its width instead of being squeezed flat",
        # Every column is frozen and saved, and the table is exactly as wide as they
        # add up to, so a fixed layout cannot rescale them.
        "function syncPinnedLayout(table)" in page
        and '`${Math.round(total)}px`' in page
        and "table.cols-resized td:not(.name-cell):not(.saved-actions):not(:has(.search-actions-menu))" in page
        and "if (th.querySelector(\".col-resizer\")) return;" in page
        and "storeColumnWidth(containerId, cellIndex, width);" in page
        and "function resetColumnWidths(containerId)" in page,
    )
    check(
        "long names wrap and the Actions button is not given an ellipsis",
        "overflow-wrap: anywhere;" in page
        and "td:has(.search-actions-menu)" in page
        and 'max-width: 18rem' not in page,
    )
    check(
        "the schedule start/stop hint is short",
        "Start and stop fill from now" in page
        and "staggers each session from now" in page
        and "1 day = now until this time tomorrow" not in page,
    )
    check(
        "site headers and counts follow the filtered rows",
        "function visibleBoxes(selector, root = document)" in page
        and "const boxes = visibleBoxes(itemSelector);" in page
        and '${site} (${visible} of ${rows.length})' in page,
    )


def main() -> int:
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
            except Exception as exc:  # a broken test is a failure, not a crash
                FAILURES.append(f"{name} raised {type(exc).__name__}: {exc}")
    if FAILURES:
        print(f"FAILED {len(FAILURES)} of {PASSED + len(FAILURES)} checks:")
        for line in FAILURES:
            print(f"  ✗ {line}")
        return 1
    print(f"OK — {PASSED} checks passed (version {VERSION})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
