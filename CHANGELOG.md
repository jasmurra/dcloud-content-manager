## 1.18.3 — 2026-09-21

- **A card stuck on stopping is refreshed again.** Ending used to skip the dCloud lookup, so a session that started back up under the same ID (or a stale public Stopping next to a signed Starting) kept saying it was being torn down. Refresh and the background watcher now follow it back to starting/active

## 1.18.2 — 2026-09-21

- **Live-session share search now uses DSX first**, the same query dCloud's share box sends. Partner emails such as `mcupid@vqcomms.com` were missing on SJC/SNG/SYD because the tool searched all cisco.com users instead. If DSX has no match, live sessions still try that broader search; saved content stays on DSX only
- Collapsed session cards show the session ID without a `#` in front of it

## 1.18.1 — 2026-09-21

- **Card Actions sits on the collapsed session row**, next to Open session, so you can save, extend, end, or move a card without expanding it. The same menu is on job workspace and monitoring cards
- Coworker zips on the Desktop are now **`-full`** (first install: app plus Python for Apple Silicon and Intel) and **`-update`** (smaller overlay onto an existing folder)

## 1.18.0 — 2026-09-21

- **Every cross-DC list now has the instant filter Search dCloud has.** Type in it to narrow *Your saved content* (both the Manage section and the Content Automation Hub), the job workspace and Session monitoring session pickers, and the Events sessions. Datacenter headers show how many of their rows match, and a group with no matches collapses out of the way
- **Check all takes only the rows the filter is showing**, so you can filter to one lab, check everything, and schedule, attach, delete, reset, or end that set as a batch
- Checked rows survive a filter change instead of being cleared, and the line under the filter box says how many checked rows are currently hidden
- **Table columns can be dragged wider.** Every list column has a divider in its header — drag it if you want a wider name, double-click any divider to go back to the automatic widths. Long names wrap in the space the panel already has instead of being cut off, so you do not have to drag the table off the page to copy a title. The Actions button stays whole (no leftover ".."), every column keeps the width you gave it, and a table dragged wider than its panel still scrolls sideways inside it rather than spilling past the edge. Each list remembers its own widths in the browser, and every datacenter table in that list stays lined up
- A state dCloud repeats on some shared content is now listed once. `saved, promoted, shared, promoted` reads `saved, promoted, shared` in both the saved-content lists and Search dCloud. `edited` and `published` are dCloud states too and still show
- Dropped the **TBv3** badge from the State column — almost all saved content is v3, and the v2 items already have their own EOL-only section — which gives the state text and the columns beside it more room
- **Find saved content no longer arrives with rows already checked.** It used to tick every row that also appears in the Content Automation Hub list, which left a scattered selection sitting under the bulk buttons. **Recheck saved IDs** still does it when you ask for it
- The schedule start/stop hint is a few short sentences instead of a paragraph covering every edge case

## 1.17.3 — 2026-09-21

- **Go to demo no longer strands TBv3 content in the v2 topology builder.** On session and event rows it now opens the parent content under **Search dCloud → Content**, the same place dCloud's own Go to demo lands, loading that datacenter's Content list first if it is not already loaded. That row's **Edit topology** already points at whichever builder owns the content, so v3 content goes to TBv3

## 1.17.2 — 2026-09-21

- Compact session rows now keep status and session ID visible when the name is long, and the **Open session** link sits in that row instead of the end timestamp. The end time remains in the expanded card
- Refresh, rename, and remove sit in a dedicated expanded-card toolbar so the pencil no longer overlaps the close button, and the long title is not repeated under the summary

## 1.17.1 — 2026-09-21

- **Job workspace now uses the same card toolbar as Session monitoring.** Check all, Expand/Collapse all DCs, and Expand/Collapse all cards sit above the compact per-DC cards instead of buried in the bulk-actions block

## 1.17.0 — 2026-09-21

- **Job workspace and monitoring cards are compact and grouped by datacenter.** Each site has its own collapsible section, and every session starts as a one-line summary with name, status, session ID, and end time; expand it for all existing VM details and actions
- **Drag session cards to reorder them within a datacenter.** Workspace and monitoring order are saved separately in the browser and survive status refreshes and page reloads
- Added **Expand all cards** and **Collapse all cards** controls to both the job workspace and Session monitoring
- The save-description field is now a multiline editor with a live character counter. dCloud TBv3 enforces a 255-character description limit, so the UI now states that limit instead of looking like an arbitrarily short one-line tool field

## 1.16.0 — 2026-09-21

- **One button signs you in to dCloud.** *Log in to dCloud* now uses the tool-owned Chromium window, the same profile CAMGR and CAI use. **Import dCloud token from browser** is gone, along with the popup that had to be watched and the Chrome Local Storage scan behind it
- **Nothing reads Chrome cookies for a dCloud token any more.** The helper that decrypted Chrome Safe Storage to recover a refresh token has been removed, so no code path can raise a Keychain prompt
- Sign-in starts at the Cisco SSO authorize URL instead of the dCloud home page. An anonymous visit to `dcloud2-<site>.cisco.com` only redirects to the public marketing site, which never sets a token, so the silent path could never succeed
- **A silent renewal gives up in about 6 seconds** once it lands on a Cisco/Duo login page, rather than waiting out the full timeout. Page load no longer starts a browser at all — it shows the saved session and lets the server renew on demand
- Fixed a bug where clicking *Log in to dCloud* could open an unexpected Chromium window and stall for up to three minutes, because the silent token snapshot behind that click was allowed to escalate to a visible sign-in

## 1.15.0 — 2026-09-21

- **Sign-in no longer reads Chrome Keychain in the background.** Status polling and CAMGR/CAI keep-alive used to decrypt Chrome cookies every few seconds, which is what produced the repeating “security wants to use Chrome Safe Storage” prompts
- **Connect to CAMGR and Connect to CAI open a tool-owned Chromium window** for Cisco SSO/Duo. That profile is stored in this install (`.dcloud-tool-chrome`) and is reused to refresh later without importing from Chrome
- Existing installs pick this up from GitHub: `start.command` installs Playwright and downloads Chromium once into `.playwright-browsers`. No new zip is required
- dCloud **Log in** still uses Cisco SSO. If the access token expires, refresh uses the saved refresh token first, then the tool browser profile if that token is gone. **Import** tries the tool browser before the Chrome cookie database
- Chrome tab / Keychain import remains a last-resort fallback if Chromium cannot finish SSO

## 1.14.3 — 2026-09-21

- dCloud admin session status **95** now displays as **VC Unavailable** instead of `95`. These broken sessions are read-only: only Info, Logs, and Go to demo are offered; they cannot be selected for bulk Reset or End
- Admin session status **99** now displays as **Error** instead of `99`. Error sessions offer Info, Reset, Logs, and Go to demo, matching the useful actions in dCloud; they cannot be ended or added to the job workspace/session monitoring
- Events now includes Info and Logs in each session's Actions menu, and its bulk buttons disable themselves when the checked sessions do not support that operation
- After a successful Event Reset or End, the tool refreshes that event every 10 seconds for up to 20 minutes. Reset tracking follows Stopping/Error/Starting back to two fresh Active reads; End tracking stops when the selected sessions have ended or left the event

## 1.14.2 — 2026-09-21

- **Reset and End now work on a session you do not own.** dCloud's `/api/sessions/{id}` route only acts on your own sessions, so an admin acting on someone else's session got "The content you are trying to access has either been removed or you do not have the permission required to view it" — even though the same admin could read that session. Both actions now retry on `/api/admin/sessions/{id}` after a permission error
- This applies everywhere Reset and End are offered: Events, Search dCloud, and the session cards. A session you own is unaffected and still uses the plain route

## 1.14.1 — 2026-09-21

- **Bulk Reset and End in Events go out one session at a time**, 1 second apart by default, instead of firing every session at once. The gap is adjustable from 0 to 30 seconds in the Events toolbar
- **A failed bulk action now says why.** A batch that failed used to report only `0 succeeded · 20 failed`; dCloud's reason for each session is now shown on screen and written to the Log

## 1.14.0 — 2026-09-21

- Added an **Events** section that finds an event by datacenter and numeric event ID, then lists every session attached to it
- Multiple event IDs can be kept at once. Results are grouped into collapsible datacenter sections with a collapsible section for each event, and the selected site/event IDs survive a page refresh
- Each event has **Check all** and **Uncheck all**, plus confirmed **Reset checked** and **End checked** actions. Finished sessions cannot be selected
- Every individual event session has an **Actions** menu for Reset, End session, View session, and Go to demo when those actions are available
- Event metadata includes status, approval, start/end, session ID, user ID, session name, demo ID, VC, session start/end, and session status
- **Refresh all** downloads the large dCloud admin Events/Sessions lists only once per datacenter and reuses them for the other event IDs in that site

## 1.13.1 — 2026-09-21

- **Saved content is grouped into a collapsible section per datacenter again**, the way Search dCloud lists its results, with **Expand all DCs** and **Collapse all DCs**. Each section remembers whether it was open
- Clicking a column header still sorts, and now sorts every datacenter's table the same way
- The saved-content columns are **Name, Content ID, Owner, State, Saved, Actions** — name first and the saved date last. The DC column is gone, since the section header names the datacenter

## 1.13.0 — 2026-09-21

- **Manage your own saved content across all DCs** replaces the separate schedule and delete lists with one combined table
- The saved-content table shows **Saved date** and sorts the complete cross-datacenter list by Saved date, Name, Content ID, DC, Owner, or State. Newest saved content appears first
- Each saved-content row now has an **Actions** menu with Schedule, Edit topology, Share, and Delete. Search dCloud no longer offers Share
- **Delete checked** permanently deletes selected deletable content across all DCs after a confirmation lists every selected name, content ID, and DC. Each deletable row also has its own confirmed Delete action
- Promoted Topology Builder v2 content remains in a separate **EOL only** section. It can be scheduled, edited, or shared, but cannot be selected for direct deletion
- Cleanup now contains session feedback surveys only; saved-content deletion moved into the Manage section

## 1.12.7 — 2026-09-21

- **Refresh no longer retires a session it could not actually read.** When the dCloud token had gone stale the signed lookup failed, and the card was ended from the unauthenticated status endpoint, which answers Deleted for anything it cannot see. A failed read now gets one retry on a fresh token; if it still fails the card stays put, says so, and the next refresh picks the session back up as Active
- **A finished card is removed from the job workspace and session monitoring instead of being hidden.** An ended card used to stay in the list invisibly, so adding that session again was refused as "already on the session cards" with nothing on screen to remove
- **A session can always be added back.** An ended card never blocks an add, and a leftover one is replaced by the new card
- **Every card that ends writes a line in the job log** saying which session it was, what dCloud reported, and which section it left. A card can no longer disappear without explanation
- **Burn-in sessions join the job already on screen.** Scheduling burn-in used to build a job of its own, which overwrote the saved job and switched the page to it — taking the job workspace and session monitoring cards off the display

## 1.12.6 — 2026-09-19

- **Extend on a session card now asks how much longer to run.** Enter `5`, `5d`, `6h`, or `5d 6h`; a bare number means days. The card no longer silently uses the one-day default from the bulk controls
- A session that takes longer than the initial startup watch no longer raises the misleading red **No sessions reached the ready state** banner. Its card keeps checking for Active, and an old copy of that message is cleared once a session is active
- **A stale start time is moved to now before the delay is applied.** Leaving the picker at 13:10 and clicking Schedule at 13:30 with a 5 minute delay now starts at 13:35, instead of sending 13:15 which dCloud treats as immediately

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
