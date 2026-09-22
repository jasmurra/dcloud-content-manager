# dCloud Content Manager

A local Mac app for scheduling dCloud sessions and managing saved content (CAI replace, CAMGR transfer, integrate, cleanup). It runs on your computer in a browser tab. Each person signs in with **their own** Cisco / dCloud account. There are no shared passwords or API keys in this repo.

## Install (coworkers)

You do not need to clone this repository or log in to GitHub.

1. Download **[dCloud-Content-Manager-Mac-full.zip](https://github.com/jasmurra/dcloud-content-manager/releases/latest/download/dCloud-Content-Manager-Mac-full.zip)** from the latest [GitHub Release](https://github.com/jasmurra/dcloud-content-manager/releases/latest). That zip includes Python for Apple Silicon and Intel.
2. Unzip it anywhere (Downloads is fine) and double-click `start.command`.
3. Sign in to dCloud in the app.

Keep the Terminal window open while you use the tool. Later versions install themselves — **Check for updates** or restart `start.command`. You do not need a new zip after the first install.

Details, troubleshooting, and how to overlay a new zip without losing jobs are in `START HERE.txt`.

## What’s new

| Version | What changed |
| --- | --- |
| **1.18.9** | Find my sessions, Find saved content, and Find events reuse a last-pull copy in this browser; Refresh downloads a live list |
| **1.18.8** | First-time install is the GitHub Release `-full` zip; open Actions menus survive background job refreshes |
| **1.18.7** | Find events by datacenter (Add or View sessions); job workspace cards survive Check for updates; save descriptions are not capped at 255 characters; Log in opens the browser immediately instead of probing first |
| **1.18.6** | Load VMs sits at the top of Schedule sessions as an optional nested block, not its own movable section |
| **1.18.5** | Top chrome buttons (What’s new, Check for updates, Settings, Sign in, Collapse/Expand/Reset) are smaller |
| **1.18.4** | Content Automation Hub saved-content rows stay unchecked after a refresh instead of ticking every box again |
| **1.18.3** | Refresh follows a stopping card if that session starts again, instead of leaving it stuck on tearing down |
| **1.18.2** | Live-session share search uses DSX first (same as dCloud), so partner emails are found |
| **1.18.1** | Session card Actions is on the collapsed row; coworker zips are named `-full` and `-update` |
| **1.18.0** | Instant filter on every cross-DC list (saved content, session pickers, Events); drag-to-resize table columns; Check all takes just the filtered rows; Find saved content no longer pre-checks Hub rows |
| **1.17.3** | *Go to demo* on session and event rows opens the parent content under Search dCloud instead of the v2 `/demo/{id}` page, so TBv3 content reaches TBv3 |
| **1.17.2** | Compact cards keep status/session ID visible on long names, put Open session on the summary row, and stop the rename pencil overlapping refresh/close |
| **1.17.1** | Job workspace card toolbar matches Session monitoring: check all, expand/collapse DCs, and expand/collapse cards sit above the compact per-DC list |
| **1.17.0** | Compact session cards grouped by DC, expandable to full details, drag-reorderable within each site; multiline save description with dCloud’s 255-character limit shown |
| **1.16.0** | One *Log in to dCloud* button through the tool browser; Import removed, no Chrome cookie reads remain, and a silent renewal gives up fast instead of stalling |
| **1.15.0** | Tool-owned Chromium for CAMGR/CAI/dCloud refresh — GitHub update installs it; no Chrome Keychain prompts in the background |
| **1.14.3** | Session status 95 is labeled VC Unavailable and limited to Info/Logs/Go to demo; status 99 is labeled Error and offers Info/Reset/Logs/Go to demo |
| **1.14.2** | Reset and End work on sessions you don't own: both retry on the dCloud admin route after a permission error, so an admin can act on another user's event sessions |
| **1.14.1** | Bulk Reset/End in Events are paced one session at a time (1s apart by default, adjustable), and a failed batch now reports dCloud's reason per session instead of only a count |
| **1.14.0** | Added Events: save multiple site/event-ID lookups in nested collapsible sections, list all event sessions, and use confirmed individual or checked Reset/End actions |
| **1.13.1** | Saved content is back in a collapsible section per DC (like Search dCloud) with Expand/Collapse all; one column click sorts every DC, and columns run Name → Content ID → Owner → State → Saved → Actions |
| **1.13.0** | Manage saved content across every DC in one sortable list with Saved date, row Actions, confirmed row/bulk delete, and a protected EOL-only section; Cleanup is surveys only |
| **1.12.7** | Refresh keeps a card it could not read instead of ending it from the public status; finished cards are removed rather than hidden, so a session can be added back; burn-in joins the job on screen instead of replacing it |
| **1.12.6** | Card Extend asks for days/hours; stale schedule start times move to now before delay; slow-starting sessions keep checking instead of showing “No sessions reached the ready state” |
| **1.12.5** | Extend by days and hours in one click, and take the farthest available time when resources are booked; delay spaces every session; copies of one demo stay 4 minutes apart; saved-content scheduling is its own card, not under Cleanup |
| **1.12.4** | After 10 minutes of guest shutdown, choose keep waiting or hard power off remaining VMs; UC guests are not yanked on a timer |
| **1.12.3** | vCUBE VMs are powered off during Guest shutdown & save, so the save no longer waits forever on a VM that restarts |
| **1.12.2** | A re-submitted CAMGR transfer is followed instead of leaving the row stuck on the first attempt's ERROR |
| **1.12.1** | dCloud sign-in is refreshed in the background, so burn-in still schedules after a long transfer and integrate |
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

GitHub’s usual pattern is this changelog file in the repo. A [Release](https://github.com/jasmurra/dcloud-content-manager/releases/latest) holds the first-time **`-full`** zip. People who already have 1.5+ do not need that zip — they only need a newer `VERSION` on `main`.

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
| `share-for-mac.command` | Builds `dCloud-Content-Manager-Mac-update.zip` (app files only) |
| `share-for-mac-with-python.command` | Builds `dCloud-Content-Manager-Mac-full.zip` (app plus Python for both Mac chips) |
| `show_usage.py` | Prints how many distinct installs have checked GitHub for updates (hashed ids only, no names) |
| `collect_usage.py` | Merges those pings into `usage.json` (also runs on GitHub Actions every few hours) |

To ship a new version: add a heading in `CHANGELOG.md`, bump `VERSION`, commit and push `main`, then tag `v` plus that version (for example `v1.18.8`) and push the tag. GitHub Actions attaches `dCloud-Content-Manager-Mac-full.zip` and `-update.zip` to that Release. People who already have 1.5+ still update from GitHub without a zip. Rebuild/upload zips only so a first-time install (`-full`) or a one-time overlay (`-update`) stays current.

## What is not in GitHub

`.env`, `.dcloud-*.json`, `last-job.json`, and `.venv` are gitignored. They hold local logins and job state and must not be committed or zipped.
