# dCloud Content Manager

A local Mac app for scheduling dCloud sessions and managing saved content (CAI replace, CAMGR transfer, integrate, cleanup). It runs on your computer in a browser tab. Each person signs in with **their own** Cisco / dCloud account. There are no shared passwords or API keys in this repo.

## Install (coworkers)

You do not need to clone this repository or log in to GitHub.

1. Unzip the folder you were sent (the larger zip includes Python).
2. Double-click `start.command`.
3. Sign in to dCloud in the app.

Keep the Terminal window open while you use the tool. Details, troubleshooting, and how to overlay a new zip without losing jobs are in `START HERE.txt`.

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

To ship a new version: bump `VERSION`, commit and push `main`, then people who already have 1.5+ update from GitHub. Rebuild zips only for a first-time install or a one-time overlay onto an older copy.

## What is not in GitHub

`.env`, `.dcloud-*.json`, `last-job.json`, and `.venv` are gitignored. They hold local logins and job state and must not be committed or zipped.
