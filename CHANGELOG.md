# What’s new

Newest version first. The same list is in the app under **What’s new**.

This file is the source of truth. GitHub also has a Releases page if someone later tags a version; the notes should match this file.

## 1.6 — 2026-09-15

- **What’s new** in the app (next to the version) and this changelog on GitHub
- README on the GitHub repo (install, updates, maintainer-only zip scripts)
- GitHub updates no longer copy zip-builder scripts (`pack_for_mac.py`, `share-for-mac*.command`) into coworker folders

## 1.5 — 2026-09-15

- **Check for updates** in the app (installs a newer version without restarting Terminal by hand)
- Public GitHub updates use a specific commit so coworkers do not get a stale version number

## 1.4 — 2026-09-15

First version published on GitHub. After this, `start.command` can install later versions over HTTPS (no GitHub login).

Included in that first shared Mac build:

- Schedule sessions and keep a job workspace
- Session monitoring
- CAI VM replace with per-VM status; CAMGR transfer, integrate, and cleanup
- Search: add a session to the job workspace or monitoring; filter by session status
- Movable and collapsible sections; compact Sign in to dCloud
- Coworker zips with or without bundled Python
