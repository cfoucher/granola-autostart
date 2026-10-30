# Granola Autostart

Starts [Granola](https://granola.ai) when a meeting begins on this PC. A small card appears first in the top-right corner of the screen under the pointer, with the meeting title and a 10-second ring.

**Cancel** leaves Granola alone. **Start now** or doing nothing opens that meeting's note and starts transcription. It does not join Zoom. The card never takes keyboard focus, so typing in another app is not interrupted (and Esc is not used).

It runs in the background, so this still works when Granola was quit or crashed. The same card is used for a calendar meeting (including one where you are in the room and not on Zoom), a Zoom join, and Granola closing mid-meeting.

Notes start only when the Windows session is unlocked and you have used the mouse or keyboard in the last few minutes. A locked or idle laptop does not start a recording. If you sit down after the start time, the card appears then, until the meeting's scheduled end.

Granola Autostart is an independent tool and is not made by Granola.

## Install

1. Get `GranolaAutostart-<version>-setup.exe` from the [releases](https://github.com/cfoucher/granola-autostart/releases), or from whoever shared it with you.
2. Run it. It installs for your Windows user only, with no admin rights, to `%LOCALAPPDATA%\Programs\Granola Autostart`. The installer isn't signed yet, so if Windows SmartScreen appears, choose **More info**, then **Run anyway**.
3. Setup opens. Zoom calls work straight away. For in-person and other meetings, connect a calendar:
   - **Google Calendar:** open [Calendar settings](https://calendar.google.com/calendar/r/settings), choose your calendar under **Settings for my calendars**, then **Integrate calendar**, and copy **Secret address in iCal format**. Google Workspace admins can turn secret addresses off; they work at Shift today.
   - **Outlook:** Settings, **Shared calendars**, **Publish a calendar**, choose **Can view all details**, then copy the **ICS** link.

   Paste it, choose **Test address**, check the meetings it found, then **Finish**.

It starts again whenever you sign in to Windows. Look for the green circle in the system tray.

### Settings

Left-click the tray icon, or right-click it and choose **Settings…**.

- **Calendars:** add, test, and remove calendars. Each shows how many meetings it has in the next day, or why its downloads are failing. Secret addresses are encrypted for your Windows user (DPAPI) and never written to the log.
- **Declined meetings** are skipped. The address comes from your calendar (a Google calendar is named after its account); add another address for aliases or Outlook calendars.
- **Starting notes:** countdown length (with **Preview**), how recently you must have used the PC, and words that skip a meeting (`focus time`, `out of office`, and `lunch` to start with). All-day events are always skipped.
- **About:** version, whether it's running, the last time it started Granola, and the log folder.

Changes save as you make them, and the running app picks them up within a few seconds.

To start notes for only some meetings on another calendar, without blocking time on your main calendar, create a calendar such as "Granola watch" in your own account and add its secret address. Then put each meeting you care about on it: invite the calendar's address (`…@group.calendar.google.com`, under Integrate calendar) if you can edit the meeting, or create your own event there with the same title and time, marked Free. The same meeting on two calendars only prompts once.

Calendars are downloaded every 5 minutes while you are at the PC, and again when you come back. A meeting added a few minutes before it starts can take that long to appear.

### Tray and notifications

- **Pause for 1 hour** or **Pause for today** — no new cards, and a countdown already on screen will not start Granola. A Zoom join during the pause is not asked about later. The icon turns grey.
- **Resume**, **Settings…**, **Open log folder**, **About**, **Quit** (it starts again at the next sign-in, or from the Start menu).

When something needs fixing, the icon gets an orange dot and Windows shows a notification once:

- **Granola didn't open** 30 seconds after a start. Granola's launch link is undocumented, so an update could break it; **Try again**, or open Granola yourself.
- **A calendar isn't updating** after 3 failed downloads in a row.
- **Settings couldn't be read**, after a hand edit broke `config.json`. Zoom joins still start notes meanwhile.

Granola's own one-minute reminder is unchanged. Clicking that reminder still opens the video call. Turn it off in Granola's notification settings if the second popup is in the way.

### Uninstall

**Settings**, **Apps**, **Installed apps**, **Granola Autostart**, **Uninstall**. It stops the app and asks whether to keep your settings.

Files: `%LOCALAPPDATA%\granola-autostart` holds `config.json`, `state.json`, `status.json`, and the logs (`log.txt`, `supervisor-log.txt`, `settings-log.txt`, `cli-log.txt`). Logs record meeting titles, never calendar addresses.

## Development

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements-build.txt
.venv\Scripts\python -m pytest
```

Running from source:

```powershell
.venv\Scripts\python watcher.py --check            # presence, Zoom, and calendar status
.venv\Scripts\python watcher.py --preview-notice   # show the card once
.venv\Scripts\python watcher.py --dry-run          # the real watcher, logging the Granola link instead of opening it
.venv\Scripts\python settings_app.py               # Settings (--setup for first-run setup)
```

Only one watcher runs at a time, so stop an installed copy first (right-click the tray icon, **Quit**). `watcher.py --install` and `--uninstall` still set up a source checkout to start at sign-in, without the installer.

### Building the installer

With the build requirements and Inno Setup 7 (6 also works) installed:

```powershell
.\build.ps1
```

It runs the tests, builds `dist\GranolaAutostart\` with PyInstaller, then `dist\GranolaAutostart-<version>-setup.exe`. One `GranolaAutostart.exe` runs every mode: the watcher, `--supervise`, `--notice`, `--settings`, `--setup`, and `--stop`.

GitHub Actions runs the tests and builds the installer for pull requests, pushes to `main`, and version tags, keeping it for 7 days. A tag such as `v1.0.0` must match `app_version.py`, and also creates a GitHub release with the installer attached.

The design canvas and the plan behind this are in [docs/installable-app-plan.md](docs/installable-app-plan.md).
