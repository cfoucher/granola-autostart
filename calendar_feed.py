"""Turn a private ICS feed into the event shape Granola's note launcher expects."""

from __future__ import annotations

import logging
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from icalendar import Calendar
import recurring_ical_events

log = logging.getLogger("granola_autostart")


@dataclass(frozen=True)
class CalendarEvent:
    id: str
    summary: str
    start: datetime
    end: datetime

    def key(self) -> str:
        return f"event:{self.id}:{self.start.isoformat()}"

    def payload(self) -> dict:
        return {
            "id": self.id,
            "summary": self.summary,
            "start": {"dateTime": self.start.isoformat()},
            "end": {"dateTime": self.end.isoformat()},
        }


def fetch_ics(url: str, timeout: float = 20) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "granola-autostart"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def events_between(
    ics_text: str,
    window_start: datetime,
    window_end: datetime,
    *,
    ignore_title_substrings: tuple[str, ...] = (),
    user_email: str = "",
) -> list[CalendarEvent]:
    calendar = Calendar.from_ical(ics_text)
    local_zone = datetime.now().astimezone().tzinfo or timezone.utc
    series_uids = {
        str(component.get("uid") or "").strip()
        for component in calendar.walk("VEVENT")
        if component.get("rrule") and str(component.get("uid") or "").strip()
    }
    found: list[CalendarEvent] = []
    instances = recurring_ical_events.of(calendar).between(window_start, window_end)
    for component in instances:
        event = _component_to_event(
            component,
            local_zone=local_zone,
            ignore_title_substrings=ignore_title_substrings,
            user_email=user_email,
            series_uids=series_uids,
        )
        if event is not None and event.end > event.start:
            found.append(event)
    found.sort(key=lambda item: item.start)
    return found


def _component_to_event(
    component,
    *,
    local_zone,
    ignore_title_substrings: tuple[str, ...],
    user_email: str,
    series_uids: set[str],
) -> CalendarEvent | None:
    status = str(component.get("status") or "").strip().upper()
    if status in {"CANCELLED", "CANCELED"}:
        return None
    if _declined(component, user_email):
        return None
    start_value = _raw_dt(component.get("dtstart"))
    if not isinstance(start_value, datetime):
        return None
    end_value = _raw_dt(component.get("dtend"))
    if isinstance(end_value, datetime):
        end = _as_aware(end_value, local_zone)
    else:
        duration = component.get("duration")
        if duration is not None and getattr(duration, "dt", None):
            end = _as_aware(start_value, local_zone) + duration.dt
        else:
            end = _as_aware(start_value, local_zone) + timedelta(hours=1)
    start = _as_aware(start_value, local_zone)
    summary = str(component.get("summary") or "").strip() or "Meeting"
    if any(piece.casefold() in summary.casefold() for piece in ignore_title_substrings if piece):
        return None
    uid = str(component.get("uid") or "").strip()
    if not uid:
        uid = f"{summary}-{start.isoformat()}"
    recurrence = _raw_dt(component.get("recurrence-id"))
    recurrence_start = recurrence if isinstance(recurrence, datetime) and uid in series_uids else None
    return CalendarEvent(
        id=_granola_event_id(uid, recurrence_start),
        summary=summary[:200],
        start=start,
        end=end,
    )


def _granola_event_id(uid: str, recurrence_start: datetime | None) -> str:
    """Match Google Calendar instance ids when the feed is a Google secret address.

    A normal event's iCal UID is ``{id}@google.com``. A recurring instance's API id is
    ``{id}_{UTC start}``. Granola looks notes up by that id plus the start time.
    """
    if uid.casefold().endswith("@google.com"):
        base = uid[: -len("@google.com")]
        if recurrence_start is None:
            return base
        stamp = recurrence_start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{base}_{stamp}"
    return uid


def _declined(component, user_email: str) -> bool:
    if not user_email:
        return False
    want = user_email.casefold()
    attendees = component.get("attendee")
    if attendees is None:
        return False
    if not isinstance(attendees, list):
        attendees = [attendees]
    for attendee in attendees:
        email = str(attendee).split(":", 1)[-1].strip().casefold()
        partstat = str(attendee.params.get("PARTSTAT", "")).upper()
        if email == want and partstat == "DECLINED":
            return True
    return False


def _raw_dt(prop):
    if prop is None:
        return None
    value = getattr(prop, "dt", None)
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return value
    return None


def _as_aware(value: datetime, local_zone) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=local_zone)
    return value.astimezone(local_zone)
