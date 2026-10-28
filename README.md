# Granola autostart

Starts [Granola](https://granola.ai) when a meeting begins on this PC. A small notice appears first:

**Granola starting in 10s for {meeting}**

Cancel or Esc leaves Granola alone. If you do nothing, it opens that meeting's note and starts transcription. It does not join Zoom.

It runs in the background, so this still works when Granola was quit or crashed. The same notice is used for a calendar meeting (including one where you are in the room and not on Zoom), a Zoom join, and Granola closing mid-meeting.

Notes start only when the Windows session is unlocked and you have used the mouse or keyboard in the last few minutes. A locked or idle laptop does not start a recording. If you sit down after the start time, the notice appears then, until the meeting's scheduled end.

## Setup

From this folder:

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python watcher.py --install
```

`--install` starts the watcher now and again at logon. It uses Task Scheduler when Windows allows it, including a once-a-minute restart if the watcher exits. If Task Scheduler denies the task, it uses your personal startup list instead, and a small supervisor starts the watcher again a few seconds after it exits. The tray icon is a green circle.

Edit the config it creates at `%LOCALAPPDATA%\granola-autostart\config.json`.

### Calendar feed

Zoom joins work without this. In-person meetings need a private ICS address:

- Google Calendar: Settings, the calendar, **Integrate calendar**, **Secret address in iCal format**.
- Outlook: calendar settings, publish or share an ICS link.

Put that URL in `ics_url`. It stays in the local config and is not written to the log. Set `user_email` as well so events you have declined are skipped.

All-day events are ignored. So are titles containing `focus time`, `out of office`, or `lunch` (change `ignore_title_substrings` if you want those).

Google event ids are taken from the ICS UID so Granola can reopen the note it already created for that event. If a second note appears for the same meeting, say so and the id mapping can be adjusted.

## Tray

- **Pause for 1 hour** or **Pause for today** — no new notices. A Zoom join during the pause is not asked about later.
- **Resume**
- **Quit** — it starts again shortly. Use `--uninstall` to stop that.

## Checks

```powershell
.venv\Scripts\python watcher.py --check
.venv\Scripts\python watcher.py --preview-notice
.venv\Scripts\python watcher.py --dry-run
.venv\Scripts\python -m pytest
```

`--dry-run` shows the real countdown and logs the Granola link instead of opening it.

Logs: `%LOCALAPPDATA%\granola-autostart\log.txt`.

## Uninstall

```powershell
.venv\Scripts\python watcher.py --uninstall
```

That removes the scheduled tasks. Quit the tray icon as well.

Granola's own one-minute reminder is unchanged. Clicking that reminder still opens the video call. Turn it off in Granola's notification settings if the second popup is in the way.
