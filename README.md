# Granola autostart

Starts [Granola](https://granola.ai) when a meeting begins on this PC. A small card appears first in the top-right corner of the screen under the pointer, with the meeting title and a 10-second ring.

**Cancel** leaves Granola alone. **Start now** or doing nothing opens that meeting's note and starts transcription. It does not join Zoom. The card never takes keyboard focus, so typing in another app is not interrupted (and Esc is not used).

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

Edit the config it creates at `%LOCALAPPDATA%\granola-autostart\config.json`. If the file has a mistake, a warning appears and the watcher runs with default settings (Zoom joins only) until it is fixed.

### Calendar feed

Zoom joins work without this. In-person meetings need a private ICS address:

- Google Calendar: Settings, the calendar, **Integrate calendar**, **Secret address in iCal format**.
- Outlook: calendar settings, publish or share an ICS link.

Put that URL in `ics_url`. Extra calendars go in `ics_urls`. Addresses must be `https://` (`webcal://` is converted). They stay in the local config and are not written to the log. Set `user_email` as well so events you have declined are skipped.

Feeds are downloaded every 5 minutes while you are at the PC (`calendar_refresh_seconds`), and again when you come back if the last download is more than a minute old. A meeting added a few minutes before it starts can take that long to appear.

To be notified for only some meetings on another calendar, without blocking time on your main calendar, create a calendar such as "Granola watch" in your own account and add its secret iCal address to `ics_urls`:

```json
"ics_urls": ["https://calendar.google.com/calendar/ical/.../basic.ics"]
```

Then put each meeting you care about on that calendar. Either invite the calendar's address (`…@group.calendar.google.com`, under Integrate calendar) to the meeting, if you are allowed to edit it, or create your own event on the Granola watch calendar with the same title and time. Mark those events Free. Your main calendar stays open for other people to book.

Quit the tray icon after saving the config so the watcher reloads it. The same meeting on two feeds only prompts once.

All-day events are ignored. So are titles containing `focus time`, `out of office`, or `lunch` (change `ignore_title_substrings` if you want those).

Google event ids are taken from the ICS UID so Granola can reopen the note it already created for that event. If a second note appears for the same meeting, say so and the id mapping can be adjusted.

## Tray

- **Pause for 1 hour** or **Pause for today** — no new notices, and a countdown already on screen will not start Granola. A Zoom join during the pause is not asked about later.
- **Resume**
- **Quit** — it starts again shortly. Use `--uninstall` to stop that.

## Checks

```powershell
.venv\Scripts\python watcher.py --check
.venv\Scripts\python watcher.py --preview-notice
.venv\Scripts\python watcher.py --dry-run
```

`--dry-run` shows the real countdown and logs the Granola link instead of opening it. Only one watcher runs at a time, so run `--uninstall` first and `--install` afterwards.

Tests:

```powershell
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

Logs: `%LOCALAPPDATA%\granola-autostart\log.txt` (the supervisor writes `supervisor-log.txt`, and commands such as `--check` write `cli-log.txt`). The log records meeting titles. Calendar addresses are never written to it.

## Uninstall

```powershell
.venv\Scripts\python watcher.py --uninstall
```

That removes the startup entry and any scheduled tasks, and stops the watcher.

Granola's own one-minute reminder is unchanged. Clicking that reminder still opens the video call. Turn it off in Granola's notification settings if the second popup is in the way.
