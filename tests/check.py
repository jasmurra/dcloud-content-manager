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
    for maintainer_only in ("pack_for_mac.py", "share-for-mac.command", "share-for-mac-with-python.command"):
        check(
            f"updater skips maintainer file: {maintainer_only}",
            maintainer_only in update_from_github.SKIP_FILE_NAMES,
        )
    check("updater skips this tests folder", "tests" in update_from_github.SKIP_DIR_NAMES)


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

    source = (ROOT / "app.py").read_text(encoding="utf-8")
    start = source.index("def _shutdown_one_dc(")
    body = source[start : source.index("def _shutdown_job(")]
    check(
        "save is after the powered-off wait",
        body.index("wait_for_power_state") < body.index("save_session("),
    )
    check("the wait is for powered off", "want_on=False" in body)
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

    check("a single session's delay is baked into start_at", schedule_copy_offsets_minutes(60, 1) == [0])
    check("three sessions stagger by the delay", schedule_copy_offsets_minutes(15, 3) == [0, 15, 30])
    check("three sessions with no delay share a start", schedule_copy_offsets_minutes(0, 3) == [0, 0, 0])
    check("copies cap at 20", len(schedule_copy_offsets_minutes(1, 99)) == 20)

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

    days = INDEX.find('id="days"')
    delay = INDEX.find('id="sched-delay"')
    copies = INDEX.find('id="sched-copies"')
    start = INDEX.find('id="sched-start"')
    check("delay and copies sit next to duration", -1 < days < delay < copies < start)


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
const cases = [
  [0, 1, ""],
  [60, 1, "60 minutes after the start time"],
  [15, 3, "one every 15 minutes"],
  [0, 3, "at the same start time"],
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
