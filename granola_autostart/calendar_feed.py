"""Turn a private ICS feed into the event shape Granola's note launcher expects."""

from __future__ import annotations

import logging
import re
import time
import urllib.request
import zlib
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from icalendar import Calendar
import recurring_ical_events

from .net import https_opener
from .settings import clean_addresses

log = logging.getLogger("granola_autostart")
MAX_FEED_BYTES = 64 * 1024 * 1024
PREFILTER_MARGIN = timedelta(days=7)
DEFAULT_MEETING_LENGTH = timedelta(hours=1)
_EMAIL = re.compile(r"[^@\s:;]+@[^@\s:;]+\.[^@\s:;]+")


@dataclass(frozen=True)
class CalendarEvent:
    id: str
    summary: str
    start: datetime
    end: datetime
    # Its title has a skip word. It never prompts, but a call joined during it is not asked about either.
    skipped: bool = False
    guests: int = 0  # how many ATTENDEE entries it has: a meeting with none is likelier a reminder than a meeting
    # Where it came from, set once the calendar is known (calendar_cache.load_feeds): its place in the Calendars list, 0
    # for the top, and the calendar's own name.
    calendar_rank: int = 0
    calendar_name: str = ""

    def key(self) -> str:
        return f"event:{self.id}:{self.start.isoformat()}"

    def payload(self) -> dict:
        return {
            "id": self.id,
            "summary": self.summary,
            "start": {"dateTime": self.start.isoformat()},
            "end": {"dateTime": self.end.isoformat()},
        }


def merge_events(*groups: list[CalendarEvent]) -> list[CalendarEvent]:
    """Keep one copy when the same meeting is on more than one calendar. Each calendar has already left out what it
    declined, so a meeting starts notes when any calendar it is on kept it.

    The first copy stays, so the groups must come in the order of the Calendars list: the copy kept is the one on the
    calendar highest in it, and that is the calendar the meeting is ranked by."""
    by_key: dict[str, CalendarEvent] = {}
    for events in groups:
        for event in events:
            by_key.setdefault(event.key(), event)
    return sorted(by_key.values(), key=lambda event: event.start)


def normalize_feed_url(url: str) -> str:
    """Secret calendar addresses carry a credential, so only https is allowed."""
    cleaned = url.strip()
    if cleaned.casefold().startswith("webcal://"):
        cleaned = "https://" + cleaned[len("webcal://") :]
    if not cleaned.casefold().startswith("https://"):
        raise ValueError("calendar feeds must use https")
    return cleaned


_opener = https_opener()


def fetch_ics(url: str, timeout: float = 30) -> str:
    request = urllib.request.Request(
        normalize_feed_url(url),
        headers={"User-Agent": "granola-autostart", "Accept-Encoding": "gzip"},
    )
    deadline = time.monotonic() + timeout * 4
    with _opener.open(request, timeout=timeout) as response:
        body = _read_capped(response, MAX_FEED_BYTES, deadline)
        encoding = (response.headers.get("Content-Encoding") or "").strip().casefold()
    if encoding == "gzip":
        body = _gunzip_capped(body, MAX_FEED_BYTES)
    return body.decode("utf-8", errors="replace")


def _read_capped(response, limit: int, deadline: float) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("calendar download took too long")
        # read1 gives what has arrived, not a whole chunk, so a server that trickles can't outlast the deadline: it is
        # looked at after every read, and the socket's timeout ends a read that gets nothing.
        chunk = response.read1(64 * 1024)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise ValueError("calendar feed is too large")
        chunks.append(chunk)


def _gunzip_capped(body: bytes, limit: int) -> bytes:
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    data = inflater.decompress(body, limit + 1)
    if len(data) > limit or inflater.unconsumed_tail:
        raise ValueError("calendar feed is too large")
    if not inflater.eof:
        raise ValueError("calendar download was cut short")
    return data


def prefilter_ics(ics_text: str, window_start: datetime, window_end: datetime) -> str:
    """Drop one-off events far from the window before the slow full parse.

    Recurring masters and moved instances are always kept, because either can
    produce or hide an occurrence inside the window.
    """
    low = (window_start - PREFILTER_MARGIN).strftime("%Y%m%d")
    high = (window_end + PREFILTER_MARGIN).strftime("%Y%m%d")
    kept: list[str] = []
    block: list[str] | None = None
    # Only "\n" ends an iCalendar line; splitlines() would also split on characters inside descriptions.
    for line in ics_text.split("\n"):
        if block is None:
            if line.startswith("BEGIN:VEVENT"):
                block = [line]
            else:
                kept.append(line)
            continue
        block.append(line)
        if line.startswith("END:VEVENT"):
            if _block_may_matter(block, low, high):
                kept.extend(block)
            block = None
    if block:
        kept.extend(block)
    return "\n".join(kept)


def _block_may_matter(block: list[str], low: str, high: str) -> bool:
    fields: dict[str, str] = {}
    depth = 0
    for line in block[1:-1]:
        if line.startswith("BEGIN:"):
            depth += 1
        elif line.startswith("END:"):
            depth = max(0, depth - 1)
        elif depth == 0:
            name = line.split(":", 1)[0].split(";", 1)[0].strip().upper()
            fields.setdefault(name, line.rsplit(":", 1)[-1].strip())
    start = _stamp(fields.get("DTSTART"))
    if start is None or "RDATE" in fields:
        return True
    if "RRULE" in fields:
        until = _stamp(_rrule_part(fields["RRULE"], "UNTIL"))
        return start <= high and (until is None or until >= low)
    if "RECURRENCE-ID" in fields:
        moved_from = _stamp(fields["RECURRENCE-ID"])
        return moved_from is None or low <= moved_from <= high or low <= start <= high
    return low <= start <= high


def _stamp(value: str | None) -> str | None:
    if not value:
        return None
    stamp = value[:8]
    return stamp if len(stamp) == 8 and stamp.isdigit() else None


def _rrule_part(rule: str, name: str) -> str | None:
    for part in rule.split(";"):
        key, _, value = part.partition("=")
        if key.strip().upper() == name:
            return value.strip()
    return None


def feed_name(ics_text: str) -> str | None:
    """The calendar's own name (X-WR-CALNAME), such as "Granola watch" or an account address."""
    # A long header line continues on the next line after a leading space or tab.
    header = re.sub(r"\r?\n[ \t]", "", ics_text.split("BEGIN:VEVENT", 1)[0])
    for line in header.split("\n"):
        name, _, value = line.partition(":")
        if name.split(";", 1)[0].strip().upper() == "X-WR-CALNAME":
            value = value.strip().replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
            return value[:200] or None
    return None


def feed_owner(ics_text: str) -> str | None:
    """The address in X-WR-CALNAME, when the calendar is named after one.

    Google names a main calendar after its account, and invites list that same address.
    A secondary calendar such as "Granola watch" has a plain name and returns None.
    """
    name = feed_name(ics_text)
    return name.casefold() if name and _EMAIL.fullmatch(name) else None


def declined_addresses(declined_by: tuple[str, ...] | None, owner: str | None, known_owners, user_email: str) -> tuple[str, ...]:
    """The addresses one calendar's declines are checked against, in lower case and once each.

    A list (``declined_by``) is exactly those addresses, and an empty one is none. Automatic (None) is the
    calendar's own address when it has one, otherwise every address the app knows, and in both cases the old ``user_email``.
    """
    if declined_by is not None:
        return clean_addresses(declined_by, lower=True) or ()
    return clean_addresses([*([owner] if owner else known_owners), user_email], lower=True) or ()


def events_between(
    ics_text: str,
    window_start: datetime,
    window_end: datetime,
    *,
    ignore_title_substrings: tuple[str, ...] = (),
    user_emails: Collection[str] = frozenset(),
    keep_skipped: bool = False,
) -> list[CalendarEvent]:
    """Meetings in the window. With ``keep_skipped``, ones with a skip word stay in, marked ``skipped``.

    ``user_emails`` are the addresses a decline is looked for in, in lower case: meetings declined by one of them are left out.
    """
    user_emails = frozenset(user_emails)
    calendar = Calendar.from_ical(prefilter_ics(ics_text, window_start, window_end))
    _give_missing_ends_an_hour(calendar)
    # Starts and ends are written in today's UTC offset, which stored event keys already use.
    local_zone = datetime.now().astimezone().tzinfo or timezone.utc
    series_uids = {
        str(component.get("uid") or "").strip()
        for component in calendar.walk("VEVENT")
        if (component.get("rrule") or component.get("recurrence-id")) and str(component.get("uid") or "").strip()
    }
    found: list[CalendarEvent] = []
    instances = recurring_ical_events.of(calendar).between(window_start, window_end)
    for component in instances:
        event = _component_to_event(
            component,
            local_zone=local_zone,
            ignore_title_substrings=ignore_title_substrings,
            user_emails=user_emails,
            series_uids=series_uids,
        )
        if event is None or event.end <= event.start:
            continue
        if keep_skipped or not event.skipped:
            found.append(event)
    found.sort(key=lambda item: item.start)
    return found


def skip_word(summary: str, words: Iterable[str]) -> str | None:
    """The first skip word in a meeting's title, if it has one."""
    return next((word for word in words if word and word.casefold() in summary.casefold()), None)


def meetings_next_day(events: list[CalendarEvent], now: datetime) -> int:
    """How many meetings start in the next day, leaving out skipped ones: a calendar's health at a glance."""
    return sum(1 for event in events if not event.skipped and now <= event.start < now + timedelta(days=1))


def _give_missing_ends_an_hour(calendar) -> None:
    """A timed event with neither DTEND nor DURATION is zero-length by the standard.

    recurring_ical_events follows that and sets DTEND to DTSTART, so the meeting would
    be dropped. Adding the hour to the source also lets the window see one under way.
    """
    for component in calendar.walk("VEVENT"):
        if "DTEND" in component or "DURATION" in component:
            continue
        if isinstance(_raw_dt(component.get("dtstart")), datetime):
            component.add("duration", DEFAULT_MEETING_LENGTH)


def _component_to_event(
    component,
    *,
    local_zone,
    ignore_title_substrings: tuple[str, ...],
    user_emails: frozenset[str],
    series_uids: set[str],
) -> CalendarEvent | None:
    status = str(component.get("status") or "").strip().upper()
    if status in {"CANCELLED", "CANCELED"} or _declined(component, user_emails):
        return None
    start_value = _raw_dt(component.get("dtstart"))
    # recurring_ical_events always sets DTEND on an occurrence, working it out from DURATION if need be.
    end_value = _raw_dt(component.get("dtend"))
    if not isinstance(start_value, datetime) or not isinstance(end_value, datetime):
        return None
    start = _as_aware(start_value, local_zone)
    end = _as_aware(end_value, local_zone)
    summary = str(component.get("summary") or "").strip() or "Meeting"
    skipped = skip_word(summary, ignore_title_substrings) is not None
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
        skipped=skipped,
        guests=len(_attendees(component)),
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


def _attendees(component) -> list:
    """The event's ATTENDEE entries: none, one, or several, which icalendar gives as nothing, one value, or a list."""
    attendees = component.get("attendee")
    if attendees is None:
        return []
    return attendees if isinstance(attendees, list) else [attendees]


def _declined(component, user_emails: frozenset[str]) -> bool:
    if not user_emails:
        return False
    for attendee in _attendees(component):
        email = str(attendee).split(":", 1)[-1].strip().casefold()
        partstat = str(attendee.params.get("PARTSTAT", "")).upper()
        if email in user_emails and partstat == "DECLINED":
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
        try:
            value = _local_time(value)
        except (OSError, OverflowError, ValueError):
            # Windows only knows its zone's rules from 1970 to 3000.
            return value.replace(tzinfo=local_zone)
    return value.astimezone(local_zone)


def _local_time(value: datetime) -> datetime:
    """A floating time (no TZID, no Z) on this PC's clock, with that date's daylight saving rule."""
    return value.astimezone()
