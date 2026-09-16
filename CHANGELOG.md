# What’s new

Newest version first. The same list is in the app under **What’s new**.

This file is the source of truth. GitHub also has a Releases page if someone later tags a version; the notes should match this file.

## 1.9 — 2026-09-16

- **Load VMs** and **Load transfer VMs** now sit at the top of their section, next to Connect. They used to slide to the bottom once a long VM list was loaded
- Each one shows which demo it will load, for example “Loads VMs from SJC 483886 — TINY BABY. Check a different row in Saved content above to change it.”
- The note warns instead when two different demos are checked, or when a ContentDEV row is checked and Transfer VMs from Content Dev is the right tool

## 1.8.5 — 2026-09-16

- Scheduling saved content no longer insists on Load VMs first. A regular session powers its own VMs, so nothing needs to be checked
- An exported session with no VMs checked now asks “Schedule anyway?” instead of refusing, matching the main Schedule sessions button
- Queued cards that were waiting on a schedule retry no longer stall when no VMs are checked

## 1.8.4 — 2026-09-16

- You can schedule the same demo again in a datacenter that already has a session card. It used to be blocked with “still use the same content ID as an existing session card”
- That check is now a confirmation: it lists the DC and demo ID, notes you will get a second session with its own session ID, and offers **Go back** in case a leftover ID from the previous demo was still in the field

## 1.8.3 — 2026-09-16

- A session card that is still **scheduled** says **Cancel session** instead of End session, so you can tell at a glance that nothing running is being destroyed. The confirmation prompt uses the same wording
- Once the session starts, the card goes back to End session. Both do the same thing in dCloud — only the wording changes

## 1.8.2 — 2026-09-16

- Fixes Load VMs keeping its old datacenter and content ID after a refresh: a restored job was refilling those two fields. Job cards still come back; only the input boxes start empty
- The VM source also returns to **Published/Saved content** on a refresh
- “Shared with …” stays on a session card. The status refresh used to drop the line a few seconds after you shared the session

## 1.8.1 — 2026-09-16

- Every datacenter dropdown starts on the first datacenter from your Settings order after a page refresh
- Search dCloud starts with that same datacenter checked until you pick your own
- A refresh clears leftover demo, session, and content IDs (and unchecks “I already have the content IDs”) instead of restoring yesterday's values

## 1.8 — 2026-09-15

- The CAI Content Dev datacenter dropdown now updates immediately when its display order changes in Settings

## 1.7 — 2026-09-15

- Personal **Settings** under the new cog button
- Drag datacenters or use ↑/↓ to choose a display order
- The saved order applies to schedule-ID fields, dropdowns, grouped results, session cards, saved-content rows, and CAI/CAMGR destination controls
- The preference stays on that user’s browser and Mac; it does not change another coworker’s order

## 1.6 — 2026-09-15

- **What’s new** in the app (next to the version) and this changelog on GitHub
- README on the GitHub repo (install, updates, maintainer-only zip scripts)
- GitHub updates no longer copy zip-builder scripts (`pack_for_mac.py`, `share-for-mac*.command`) into coworker folders

## 1.5 — 2026-09-15

- **Check for updates** in the app (installs a newer version without restarting Terminal by hand)
- Public GitHub updates use a specific commit so coworkers do not get a stale version number

## 1.4 — 2026-09-15

First version published on GitHub. After this, `start.command` can install later versions over HTTPS (no GitHub login).

Also in that drop (after the 1.3 zips):

- Find session in the job workspace (same idea as session monitoring)
- Guest shutdown stays pending until the VM is verified off
- Faster add-to-monitoring (does not pull full session info up front)

Plus the rest of the shared Mac app at publish time: schedule sessions, monitoring, CAI replace, CAMGR transfer/integrate/cleanup, search actions, movable sections, Sign in to dCloud, zips with or without Python.

## 1.3 — 2026-09-15

Desktop zips (before GitHub). **Burn-in is this one:** after CAMGR transfer + auto-integrate, optional checkbox to spin up new demo sessions for N days (default 1).

Also in 1.3:

- `net_errors.py` in the zip (the missing-module crash) and a packer check so incomplete zips cannot be built
- `start.command`: only kills the listening server (not Chrome); crash window stays open; `sudo` / root-owned `.venv` guard
- Preloaded Content/Sessions with “how long ago” per datacenter
- Actions dropdown on search rows
- Wider Logs popup
- Session Details / Session info (NAT IPs, DNS, phones, VPN, documents)

## 1.2 — 2026-09-12

Desktop zips after catalog and naming fixes (no burn-in yet):

- Catalog IDs from the global catalog (one lookup instead of per-DC search)
- VM labels use Topology Builder display name vs vCenter/hypervisor name
- CAI integrate dest chips merge instead of wiping other DCs on a re-submit

## 1.1 — 2026-09

Overlay update without losing jobs (`last-job.json`, saved IDs, logins stay in the existing folder). Small zip and with-Python zip packed together.

## 1.0 — 2026-09

First versioned Mac zip: version number on the page, `start.command`, coworker pack without secrets.
