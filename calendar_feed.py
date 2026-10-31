"""Turn a private ICS feed into the event shape Granola's note launcher expects."""

from __future__ import annotations

import logging
import re
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from icalendar import Calendar
import recurring_ical_events

log = logging.getLogger("granola_autostart")
MAX_FEED_BYTES = 64 * 1024 * 1024
PREFILTER_MARGIN = timedelta(days=7)
_EMAIL = re.compile(r"[^@\s:;]+@[^@\s:;]+\.[^@\s:;]+")


@dataclass(frozen=True)
class CalendarEvent:
    id: str
    summary: str
    start: datetime
    end: datetime
    # Its title has a skip word. It never prompts, but a Zoom join during it is not asked about either.
    skipped: bool = False

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
    """Keep one copy when the same meeting is on more than one calendar."""
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


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.casefold().startswith("https://"):
            raise urllib.error.HTTPError(req.full_url, code, "redirect away from https refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HttpsOnlyRedirects)


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
        chunk = response.read(64 * 1024)
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


def my_addresses(user_email: str, owners) -> frozenset[str]:
    """Every address declines are checked against: the typed one plus each calendar's own."""
    found = {address.strip().casefold() for address in (user_email, *owners) if address and address.strip()}
    return frozenset(found)


def events_between(
    ics_text: str,
    window_start: datetime,
    window_end: datetime,
    *,
    ignore_title_substrings: tuple[str, ...] = (),
    user_emails: frozenset[str] = frozenset(),
    keep_skipped: bool = False,
) -> list[CalendarEvent]:
    """Meetings in the window. With ``keep_skipped``, ones with a skip word stay in, marked ``skipped``."""
    calendar = Calendar.from_ical(prefilter_ics(ics_text, window_start, window_end))
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
        if event is not None and event.end > event.start and (keep_skipped or not event.skipped):
            found.append(event)
    found.sort(key=lambda item: item.start)
    return found


def _component_to_event(
    component,
    *,
    local_zone,
    ignore_title_substrings: tuple[str, ...],
    user_emails: frozenset[str],
    series_uids: set[str],
) -> CalendarEvent | None:
    status = str(component.get("status") or "").strip().upper()
    if status in {"CANCELLED", "CANCELED"}:
        return None
    if _declined(component, user_emails):
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
    skipped = any(piece.casefold() in summary.casefold() for piece in ignore_title_substrings if piece)
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


def _declined(component, user_emails: frozenset[str]) -> bool:
    if not user_emails:
        return False
    attendees = component.get("attendee")
    if attendees is None:
        return False
    if not isinstance(attendees, list):
        attendees = [attendees]
    for attendee in attendees:
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
        return value.replace(tzinfo=local_zone)
    return value.astimezone(local_zone)
