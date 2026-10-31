# Plan: Granola Autostart as an installable Windows app

Keep the current Python code and package it, rather than rewriting it. Two small fixes first, then three phases; each phase is useful on its own.

**Status (30 October 2026):** the two fixes, Phase 1, and Phase 2 are built and released as 1.0.0, with Settings and setup in WebView2. From Phase 3, the GitHub release job and the setup guide (README) are done. Still open: code signing (installers are unsigned) and the update check, which waits for installers to have a public home.

Designs: [Granola Autostart app](https://claude.ai/artifact/BuZ3FWA3LoZ8qbzzokkP69) in Claude Design (private until shared from its Share menu). It covers first-run setup, Settings, tray menu states, Windows notifications, and the countdown card, in the card's existing "Paper" style with light and dark versions.

## Current shape

- **Watcher** (`watcher.py`): tray icon plus a loop every 5 seconds that checks presence, Zoom, Granola, and the calendar.
- **Countdown card** (`notice.py`): a separate small window the watcher starts; its exit code means start or cancel.
- **Supervisor** (`watcher.py --supervise`): restarts the watcher if it exits. Started at logon from the HKCU Run key, because Task Scheduler is denied on managed machines.
- **Config** is hand-edited JSON in `%LOCALAPPDATA%\granola-autostart\config.json`. A broken file shows a message box once.
- **Tray menu**: Pause for 1 hour, Pause for today, Resume, Quit.
- **Runs from** `.venv` and a separately installed Python.
- **Repo**: `cfoucher/granola-autostart` on GitHub, private, with no workflows yet.

## Before Phase 1: two small fixes (about an hour)

1. **Card contrast and wording.** White on the card's green (`#5a8a47`) is 4.1:1, below the 4.5:1 needed for small text. Add two palette entries in `notice.py` and keep `#5a8a47` for the ring:
   - Buttons `#4b7a3a` (5.0:1 with white). Small green text, such as the eyebrow, `#42703a`.
   - Warning text `#94501a` instead of `#a3581b`, so it also passes on the warning background the Settings window uses.
   - Dark mode already passes.
   - The Granola-closed card says "GRANOLA · RESTARTING NOTES" and its button says **Start now**, like the other cards. Its orange line already explains the restart. Today it says "RESTARTING" and **Restart now**.
2. **Declined meetings, without asking for an email.** `_declined` compares attendees with one hand-entered address, `user_email`. A Google feed's `X-WR-CALNAME` is the account's own address for its main calendar, and that is the address invites use.
   - The author's `user_email` was a personal address on another domain. It matched 23 attendee entries (1 declined), against 2,470 (41 declined) for the calendar's own address. Fixed in the author's config on 30 October 2026; this change makes it automatic for everyone.
   - Build one set of the user's addresses: every feed's `X-WR-CALNAME` that is an email address, plus `user_email`. Check every feed against the whole set. That way a secondary calendar such as "Granola watch", whose name is not an address, is covered by the main calendar's address.
   - `user_email` becomes optional. It is needed only when no feed's name is an address (Outlook, or only secondary calendars), or for an alias. Existing values keep working.

## Phase 1: an installer for one person (1 to 2 days)

3. **Packaged mode.** `ui_python()`, `NOTICE`, and `quoted_command()` assume `pythonw.exe` plus script paths. In a packaged build there is one `GranolaAutostart.exe` with modes:
   - `GranolaAutostart.exe` runs the watcher.
   - `GranolaAutostart.exe --supervise` runs the supervisor.
   - `GranolaAutostart.exe --notice --title=...` shows the countdown card. `app.py` handles this mode without importing the watcher, so the card opens quickly.
   - `GranolaAutostart.exe --stop` stops that copy's supervisor and watcher, and nothing else. The installer runs it before an upgrade and before uninstalling.
   - Phase 2 adds `--settings` and `--setup`.

   Detect packaged mode (`sys.frozen`) and build the command either way, so running from source keeps working.
4. **PyInstaller, one-folder build.** A one-file build unpacks to a temp folder on every launch, which delays each countdown card by a second or two and is the usual cause of antivirus false positives. The one-folder build is about 40 MB.
5. **Per-user Inno Setup installer** (`PrivilegesRequired=lowest`), using Inno's standard wizard.
   - Installs to `%LOCALAPPDATA%\Programs\Granola Autostart` without admin rights.
   - Registers the HKCU Run entry, adds an Add/Remove Programs entry, and starts the app after install.
   - Adds a Start menu shortcut carrying an `AppUserModelID`. Windows notifications in Phase 2 need that ID.
   - The uninstaller stops running processes first, then asks whether to keep settings.
6. **`build.ps1`.** Runs the tests, then the PyInstaller build, then the installer compile. It works the same on a laptop and in CI.
7. **GitHub Actions build** (`.github/workflows/build.yml`) on `windows-latest`, because the tests and the build need Windows (`winreg`, `ctypes.windll`).
   - Pushes to `main`, version tags (`v*`), and pull requests run pytest, then `build.ps1`, and upload the installer as a workflow artifact. Artifacts are kept for 7 days. Building on pull requests lets an installer be tried before merging.
   - Python comes from `actions/setup-python`, pinned to the version used locally. Inno Setup is installed with `choco install innosetup` (Chocolatey is on the runner image).
   - Workflows only need the default `GITHUB_TOKEN`; no other secrets until signing.
8. **Migration.** Run `--uninstall` on the source version, then the installer. Config and state in `%LOCALAPPDATA%\granola-autostart` carry over unchanged.

### GitHub Actions cost while the repo is private

- **Free plan allowance.** GitHub Free includes $12 of runner time a month for private repos, plus 500 MB of artifact storage. Pro includes $18 and 1 GB.
- **Windows rates.** A standard Windows runner costs $0.010 a minute, rounded up per job, so the Free allowance is about 1,200 Windows minutes a month.
- **Expected use.** Estimates, to check against the first real runs: a test run takes about 3 minutes and a full build about 8. A few pushes a day stays well inside the allowance.
- **When it runs out.** With no payment method on file, Actions are blocked until the next month and nothing is charged. GitHub emails at 90% and 100% of the allowance.
- **Storage.** A 7-day artifact retention keeps installers under 500 MB. Installers worth keeping go on releases instead (item 19).
- **Public repos** would make standard runners free, which is one reason to revisit visibility later.

## Phase 2: usable by someone who isn't the author (about 5 days)

9. **Window toolkit for Settings and setup.** Recommended: an Edge WebView2 window through `pywebview`. The countdown card stays in Tk, which opens instantly and never takes focus.
   - The designs are HTML and CSS, so the markup carries over almost as is. Text fields, scrolling, keyboard focus, screen readers, and dark mode work without custom drawing.
   - Cost: about 15 MB more in the build (`pythonnet` and the WebView2 loader), and about a second to open the window.
   - WebView2 is part of Windows 11, and Windows 10 gets it through Edge updates. If it is missing, **Settings...** opens `config.json` in Notepad instead.
   - Tk instead means no new dependency, but every pill button, chip, segmented control, and list row has to be drawn on a canvas, as the card is now. That is roughly 2 to 3 more days, and harder to make accessible.
10. **Status file.** Settings has to show what the watcher knows, so the watcher writes `status.json` whenever it changes. Settings reads it every 2 seconds. It holds:
    - Per calendar feed: an id (a truncated SHA-256 of the address, never the address), the calendar's name, whether that name is an email address, the number of meetings in the next day, the last successful download, consecutive failures, and the last error (exception type or HTTP status).
    - The next meeting, the pause end, the last start (title, time, and whether Granola was seen running), and whether an update is available.
11. **Settings window**, opened from **Settings...** in the tray, 780 x 620. It fits the 1280 x 672 work area of a 1080p screen at 150%. Opening it again brings the open window forward. Changes save as they are made; there is no Save button. Three sections:
    - **Calendars**: one row per feed with its name from `X-WR-CALNAME`, its status line, **Test**, and remove. A main Google calendar's name is the account's address. **Add calendar** opens an inline panel: paste the address, **Test** shows the calendar's name and next three meetings, then **Add calendar**.
    - Below the list, **Declined meetings** says they are skipped automatically and which address is used: "Skipped automatically, using you@shift.com from your calendar." **Add another address** covers aliases. Only when no calendar's name is an address does this section show an email field instead.
    - **Starting notes**: countdown length (5, 10, 15, 20, or 30 seconds), **Preview** (shows the card, never starts Granola), idle time, and title words to skip.
    - **About**: version, update status with **Check now** (only once item 20 exists), the last start and whether Granola opened, **Open log folder**, and **Run setup again**.

    A side panel shows the watcher's state, with the tray menu's commands, so nothing in the tray is missing from Settings (the "Status panel states" board): Watching or Taking notes with **Pause** (1 hour, rest of today), Paused with **Resume**, Needs attention with **Fix**, Not running with **Start**, and Starting… for the seconds before a new watcher reports. **Quit** is in About. Settings sends these to the watcher as named Windows events (`commands.py`), and the watcher exits with code 3 after Quit so the supervisor stops instead of restarting it.
12. **First-run setup**, four steps. It opens on first run when no calendar is configured and setup has not been finished or skipped.
    - Welcome, showing the card. **Zoom only for now** skips to the end.
    - How to copy the secret address, with Google and Outlook tabs. Includes "Not there? Your admin may have turned it off."
    - The test result: the calendar's next meetings, including any skipped by a title rule, and which address declined meetings are matched to. It asks for an email only when the calendar's name is not an address (Outlook).
    - Done: where the tray icon is, and **Show me the card**.
13. **Encrypted calendar addresses.** Secret iCal addresses act like passwords, so store them with DPAPI (`CryptProtectData` through `ctypes`, per user) as `ics_urls_protected`. Plain-text `ics_url` and `ics_urls` are encrypted on the next load and removed from the file, so hand-added addresses still work.
14. **Live reload.** The watcher checks `config.json`'s modified time each tick and reloads it. Settings writes it atomically (temporary file, then replace), half a second after the last change. This replaces "Quit from the tray to reload".
15. **Tray states.** The menu stays a native Windows menu: one line per item, with no icons or colours. Each state has a greyed-out first line saying what is going on.
    - **Watching**: green icon. First line "Next: Weekly product sync, 10:00".
    - **Paused**: grey icon with pause bars. First line "Paused until 11:42", then **Resume**.
    - **Needs attention**: orange dot on the icon. First line names the problem, then **Fix in Settings...**.
    - **Update ready** (once item 20 exists): blue dot. "Version 1.1.0 is available", then **Install update...**.
    - **Settings...** is the default item in bold, so left-clicking the icon opens Settings. **Open log folder**, **About**, and **Quit** follow.
    - `pystray` supports this through dynamic item text, `enabled=False`, `default=True`, `update_menu()`, and swapping `icon.icon`.
16. **Visible failures** as Windows notifications through `windows-toasts`, replacing the message box. Each is shown once per problem, until it recovers, and also sets the tray's attention state.
    - **Granola didn't open**: not running 30 seconds after a launch. This is already detected and only logged by `_warn_if_launch_missing`. Buttons: **Try again**, **Open log folder**. This matters most because `granola://new-document` is undocumented and a Granola update could break it.
    - **Calendar not updating**: 3 failed downloads in a row. Buttons: **Open Settings**, **Dismiss**.
    - **Settings couldn't be read**: a hand edit broke `config.json`. Buttons: **Open file**, **Dismiss**.

    Notifications need an app ID: the installer's Start menu shortcut, or when running from source, the HKCU `AppUserModelId` registry key.
17. **Version.** `__version__` in `app_version.py` already sets the installer and exe version (Phase 1). Show it in About too. CI checks that a `v*` tag matches it.

## Phase 3: distribution (1 to 2 days)

18. **Code signing.** Unsigned installers trigger SmartScreen's "unknown publisher", and Defender sometimes flags PyInstaller builds. Options:
    - Shift IT signs or deploys it.
    - Microsoft Trusted Signing (about $10 a month; requires identity validation). It has a GitHub Action, so signing can happen in the build workflow.
    - Unsigned, with "More info, then Run anyway" instructions.
19. **Releases.** A version tag also creates a GitHub release with the installer attached. While the repo is private, only people with access to the repo can download it, so coworkers get the installer by direct share (for example Slack or a shared drive).
20. **Update check, once installers have a public home.** A private release can't be downloaded without a GitHub token, and a token can't be shipped inside the app. So there is no update check while the repo is private, and Settings hides **Check now**. When updates matter, pick one:
    - Make the repo public.
    - Publish installers from this workflow to a separate public releases-only repo.
    - Use a Shift-internal download location.

    Then: a daily check of the latest release. The tray shows **Update ready**, and **Install update...** downloads the installer and runs it silently. The installer stops the running app first.
21. **Setup guide.** Install, then follow first-run setup. Google Workspace admins can disable secret addresses; they work at Shift today.

## Testing

- **Windows Sandbox** (no Python installed):
  - The app starts at logon, and first-run setup opens on a clean profile.
  - The countdown appears for a calendar meeting and for a Zoom join.
  - Notifications appear, and their buttons work.
  - Uninstall leaves no processes running.
- **Settings and setup** at 100% and 150% scaling, in light and dark mode, and with only the keyboard.
- **Config migration**: a plain-text config is encrypted on first load, and decrypts after a restart.
- **Upgrade** over a running install.
- **Defender scan**, and SmartScreen behavior signed and unsigned.
- **Full pytest suite** in CI on every push. New pure logic gets tests: the status file, failure counts, decline matching, and DPAPI round trips.

## Decisions needed

- **Settings toolkit:** WebView2 (decided and built).
- **Audience:** only the author (stop after Phase 1), or coworkers.
- **Signing:** Shift IT, Trusted Signing, or unsigned.
- **Repo visibility:** private for now (decided). Revisit before the update check (item 20).
- **Name:** "Granola Autostart" uses Granola's name. That's fine internally, but a neutral name avoids trademark questions outside Shift. About already says it is not made by Granola.

## Risks

- **Granola's launch link** (`granola://new-document`) is undocumented. A Granola update could break launching for every install at once. The "Granola didn't open" notification makes that visible the same day.
- **PyInstaller builds** occasionally trigger antivirus false positives until signed.
- **Zoom detection** relies on `CptHost.exe` running during meetings. A Zoom update could change that.
- **Notification buttons** are handled by the running watcher. A click after the watcher has restarted does nothing, apart from dismissing the notification.
- **Calendar names as addresses.** Outlook calendar names are not email addresses, so Outlook-only users still type their address once. Google could also change what `X-WR-CALNAME` holds; a typed address is the fallback.
- **Actions allowance.** If builds outgrow the Free plan's $12 a month, Actions stop until the next month. Adding a payment method with a budget, or making the repo public, avoids that.
