# dCloud Content Manager

A local Mac app for scheduling dCloud sessions and managing saved content (CAI replace, CAMGR transfer, integrate, cleanup). It runs on your computer in a browser tab. Each person signs in with **their own** Cisco / dCloud account. There are no shared passwords or API keys in this repo.

## Install (coworkers)

You do not need to clone this repository or log in to GitHub.

1. Unzip the folder you were sent (the larger zip includes Python).
2. Double-click `start.command`.
3. Sign in to dCloud in the app.

Keep the Terminal window open while you use the tool. Details, troubleshooting, and how to overlay a new zip without losing jobs are in `START HERE.txt`.

## What’s new

| Version | What changed |
| --- | --- |
| **1.12** | Schedule reports its result under the button; a failed schedule leaves no card; fixed the 400 under a green Sign in button |
| **1.11.1** | A demo that is its own base shows that ID in Root ID, with no copy button |
| **1.11** | Root ID column on the Hub list, filled from CAMGR; Use root as target copies it into Target ID |
| **1.10.3** | Guest shutdown & save waits for VMs to power off, card actions match the bulk wording, and "Owned by" stops vanishing from cards |
| **1.10.2** | Session cards show the Virtual Center number from dCloud |
| **1.10.1** | Resource conflicts explain the blocker, stay within a 24-hour start window, and offer an adjustable retry |
| **1.10** | Delay (minutes) and number of sessions next to Duration; removed IDs can be added back to the Hub list |
| **1.9** | Load VMs stays at the top of its section and names the demo it will load |
| **1.8.5** | Scheduling saved content no longer requires Load VMs first |
| **1.8.4** | Scheduling the same demo again in a DC is allowed (with a confirmation) |
| **1.8.3** | Scheduled sessions offer **Cancel session** instead of End session |
| **1.8.2** | Load VMs no longer keeps its old datacenter and ID after a refresh; “Shared with” stays on the card |
| **1.8.1** | Dropdowns start on your first datacenter after a refresh; stale IDs are cleared |
| **1.8** | CAI Content Dev dropdown also updates immediately with the saved DC order |
| **1.7** | Personal Settings cog: drag datacenters into the display order used throughout the app |
| **1.6** | What’s new in the app; this README; zip-builder scripts stay off coworker installs |
| **1.5** | Check for updates in the app; public GitHub updates by commit |
| **1.4** | First GitHub release: HTTPS auto-update; find session in job workspace; shutdown verify |
| **1.3** | **Burn-in checkbox** after integrate; session info; `net_errors` in the zip; start.command fixes |
| **1.2** | Global catalog lookup; VM display vs hypervisor names; CAI dest chips merge |
| **1.1** | Overlay a zip without wiping jobs |
| **1.0** | First versioned Mac zip |

Full notes: [CHANGELOG.md](CHANGELOG.md). In the running app, use **What’s new** next to the version number.

GitHub’s usual pattern is this changelog file in the repo. Optional extra: a [Release](https://github.com/jasmurra/dcloud-content-manager/releases) per version (for example, a `v1.8` tag with the same notes). People do not need Releases to update — they only need a newer `VERSION` on `main`.

## Updates

After the 1.5 zip is installed, later versions install themselves:

- **Check for updates** at the top of the page, or
- Restart `start.command`

A newer `VERSION` on this GitHub repo is copied into the existing folder. Jobs, logins, `.venv`, and bundled Python are left alone.

## Maintainer files (not for coworker installs)

These stay in GitHub for the person who builds zips. They are **not** packed into the coworker zips, and the in-app updater does not copy them:

| File | What it is for |
| --- | --- |
| `pack_for_mac.py` | Builds the Desktop zip files |
| `share-for-mac.command` | Builds the small zip (no Python) |
| `share-for-mac-with-python.command` | Builds the large zip (Python included) |

To ship a new version: add a heading in `CHANGELOG.md`, bump `VERSION`, commit and push `main`. People who already have 1.5+ update from GitHub. Rebuild zips only for a first-time install or a one-time overlay onto an older copy.

## What is not in GitHub

`.env`, `.dcloud-*.json`, `last-job.json`, and `.venv` are gitignored. They hold local logins and job state and must not be committed or zipped.
