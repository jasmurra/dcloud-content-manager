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


def test_release_metadata() -> None:
    import app
    from update_from_github import is_newer

    check("app.py reports the VERSION file", app._read_app_version() == VERSION)
    check("VERSION looks like a release", bool(re.fullmatch(r"\d+(\.\d+)+", VERSION)), VERSION)
    check("a newer VERSION wins", is_newer(VERSION, "0.9"))
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    check(f"CHANGELOG has a {VERSION} section", f"## {VERSION}" in changelog)
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
"""
    run_node(source, "Load VMs note names the demo it will pull")


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
