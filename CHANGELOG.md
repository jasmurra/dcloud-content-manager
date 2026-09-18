## 1.12.5 — 2026-09-18

- **Delay now spaces out every session, not just extra copies.** Checking three saved demos with a 30 minute delay used to schedule all three at the same time, because the delay only applied when **Number of sessions** was above 1. Each session now waits one more delay than the one before it, in the order listed, from the saved content list and from manual demo IDs alike
- A session scheduled on its own is unchanged: the delay still pushes out its own start (60 = Now plus an hour)
- **Copies of one demo are never scheduled closer than 4 minutes**, even with the delay at 0. dCloud holds back a second session of the same demo that comes up at the same moment, so spinning one demo up 5 times now goes out at 0, 4, 8, 12, and 16 minutes. Different demos with no delay still start together
- When a capacity conflict sends a datacenter to the next open slot, its sessions keep their spacing instead of collapsing onto one start time
- **Schedule from saved content is its own card** (**Schedule sessions from your own saved content across all DCs**). Cleanup keeps Find saved content only for deleting finished copies and declining surveys
- **Extend by days and hours** from each session's current end (5 days is one click). If resources are booked after the session, the tool finds the farthest time that still fits and asks **Extend to that time?** before applying it

## 1.12.4 — 2026-09-18

- Guest shutdown now **waits as long as the VMs need**, including slow UC guests. After 10 minutes a prompt asks whether to keep waiting or hard power off remaining VMs (and save, when this was Guest shutdown & save). Walking away keeps waiting; leftover VMs are not yanked on a timer
- **Guest shutdown all powered-on VMs** on a card guest-shuts everything that is on (vCUBE is still powered off) and does not save. The card keeps reporting which VMs are still shutting down
- The card names slow UC VMs as ones that will not be powered off unless you choose that after the 10-minute prompt

## 1.12.3 — 2026-09-18

- **vCUBE is powered off instead of guest-shut-down.** dCloud accepts a guest shutdown on vCUBE and the VM restarts instead of stopping, so the save sat forever on “Waiting for VMs to shut down before saving: VCUBE”. Guest shutdown & save now sends a power off for those VMs and a real guest shutdown for everything else
- The card says which is which: “Powering off (no guest shutdown): VCUBE” alongside the normal waiting line
- A VM’s own **Guest shutdown** button is replaced with a “No guest shutdown” note on vCUBE, and the API turns a guest shutdown on one into a power off rather than letting it bounce

## 1.12.2 — 2026-09-18

- A transfer you **re-submit after a failure** is now followed properly. Each row remembers one CAMGR job, and a finished job used to win that match outright, so the row kept reporting the old **ERROR** while CAMGR showed the second attempt importing. A live job for the same demo, source DC, and owner now takes over, and the row stores the new job
- A row that reads ERROR no longer stops checking. It asks CAMGR again every couple of minutes, so a retry is picked up instead of freezing on the failure forever
- A still-running job is never second-guessed, and a transfer that really did fail with no retry still reports ERROR
- The job log says when the row switches: “following the re-submitted CAMGR transfer … The earlier attempt failed”

## 1.12.1 — 2026-09-18

- The dCloud token is now kept fresh in the background, refreshed about every 4 minutes and always before it expires. A CAMGR transfer plus CAI integrate can run for hours, and the post-integration burn-in sessions used to fail at the end because the sign-in had quietly gone stale
- Burn-in and job actions now use your **live** sign-in instead of the refresh token copied onto the job when it was created. dCloud retires that old token as soon as anything else refreshes, so the copy could be dead by the time integration finished
- Leave `start.command` running and the app tab open for a long transfer. You do not need to refresh the page — the token refresh happens in the app, not the browser

## 1.12 — 2026-09-17

- The **Schedule** button now reports back under itself: “Scheduling 2 sessions…” while dCloud works, then the session IDs it got. With Job workspace collapsed the click used to produce no visible result at all. **Show in Job workspace** opens the panel when you want the cards, so it is not a popup you have to dismiss
- A schedule that never created a session **no longer leaves a card behind** for you to close. The reason goes to the log and the error banner instead. A capacity conflict still keeps its card, because that one carries the next-available details and **Adjust schedule**
- Fixed the **400 under a green Sign in button**. dCloud retires the old refresh token each time it issues a new one, so two background threads refreshing at the same moment meant one of them spent a dead token and reported you signed out. Refreshes now happen one at a time, and a thread that waited reuses the token the winner just stored. A failed call also re-checks the button instead of waiting for the next poll
- **Guest shutdown & save** no longer says “Waiting for dCloud to report the save” while VMs are still powering off, and a card is no longer stuck on that message if the app restarts mid-shutdown. Such a card now says the shutdown was interrupted and can be run again
- Removed the leftover “Connect to your session, then guest-shutdown & save when finished.” line from healthy active cards
- Error messages and hints no longer mention **Step 1** or **Step 2**, which stopped matching the page when sign-in moved to the button at the top
- Root ID fills in more cases: dCloud spells the field `_rootDemoId` on some demos and CAMGR uses `fkrootDemoId` on others, and both are now read. Root IDs also survive an app restart instead of being blanked by the job's own copy of the row, and rows looked up by the older logic are rechecked automatically

## 1.11.1 — 2026-09-16

- A demo that is already the original base now shows **its own saved ID** in Root ID with “(this is the base)”, instead of a blank dash you had to hover to understand. There is no copy button on those rows — copying would point the demo at itself
- Those rows no longer count as missing a root, so they stop asking you to connect to CAMGR and stop being rechecked

## 1.11 — 2026-09-16

- Saved content list has a **Root ID** column (CAMGR `fkrootDemoId`, the original base after a chain of saves). Target ID stays the previous save and is what CAI replace uses
- Root stays blank until CAMGR is connected, unless a leftover lookup already stored it. A note under the table says to Connect to CAMGR when the column is empty
- **Use root as target** (toolbar and per-row) copies Root into Target when you really want to replace VMs in the original base
- Target lookup no longer silently falls back to the CAMGR root, which used to put the base demo into Target on a save-of-a-save
- A blank Root ID says why when you hover it: CAMGR has no root recorded, the demo points at itself, or the CAMGR call failed
- **Recheck root IDs** asks CAMGR again for every row. A failed CAMGR call no longer marks a row as "already looked up", which used to leave Root blank permanently

## 1.10.3 — 2026-09-16

- **Guest shutdown & save** waits until the guest-shutdown VMs actually show powered off before it submits the save. This applies to both the card action and the bulk button at the top. The card lists which VMs are still shutting down. If the wait times out after 10 minutes, it starts the save anyway so the session is not left hanging
- Card actions now use the same wording as the bulk buttons and drop the redundant “session”: **Session info**, **Share…**, **Guest shutdown & save**, **Extend**, **Reset**, **End** (or **Cancel** on a session that has not started)
- **Owned by** no longer disappears from cards. Restoring the last job after an app restart left the tool without a token to compare owners against, so every card silently dropped the line until something refreshed. It now falls back to the signed-in user and keeps the last known answer

## 1.10.2 — 2026-09-16

- Session cards show the **Virtual Center** number next to the session ID, the same value dCloud shows on its Sessions page

## 1.10.1 — 2026-09-16

- **What’s new** opens directly on the latest version instead of repeating its title and GitHub notes
- “Use a nearby slot” now means the replacement must start within 24 hours. The tool will never quietly schedule a session many days later
- A dCloud capacity error names the blocked resource, keeps the requested dates, and includes the exact technical response under a disclosure
- For a long request that cannot fit, the tool checks whether a shorter 30-, 14-, 7-, 3-, or 1-day session can start within 24 hours
- Failed schedule cards offer **Adjust schedule**. It loads the demo, DC, duration, and start/end times back into Schedule sessions for review; nothing is retried until the user clicks Schedule sessions

## 1.10 — 2026-09-16

- Next to Duration: **Delay (minutes)** defaults to 0, and **Number of sessions** defaults to 1
- A delay of 60 with one session starts an hour after the start time (Now + 60)
- Three sessions with a delay of 15 start at the chosen time, then 15 minutes later, then 15 minutes after that — each copy gets its own session ID
- The same two fields sit on Cleanup → saved content scheduling and stay in sync with Schedule sessions
- Fixes **Add to Hub** doing nothing for a demo ID you had removed from the saved content list. Remove from list recorded the ID in two places and adding it back only cleared one, so the row was stored and then filtered straight back out
- Removing a row is now durable on its own. CAI/CAMGR status sweeps re-add whatever they find in flight, and that used to quietly clear one of the two hide lists, so a removed row could come back on a later refresh

## 1.9 — 2026-09-16

- **Load VMs** and **Load transfer VMs** now sit at the top of their section, next to Connect. They used to slide to the bottom once a long VM list was loaded
- Each one shows which demo it will load, for example “Loads VMs from SJC 483886 — TINY BABY. Check a different row in Saved content above to change it.”
- The note warns instead when two different demos are checked, or when a ContentDEV row is checked and Transfer VMs from Content Dev is the right tool
- “Check a different row in Saved content above to change it” sits on its own line under the demo name, instead of wrapping mid-sentence next to the button
- **Content Integration Tasks** and **Content Transfer Tasks** now fold away by clicking their headers, like the main sections. They start open, remember how you left them, and follow Collapse all / Expand all. Show help still only flips the help text

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
