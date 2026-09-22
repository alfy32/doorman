"""Temporary people: the access window, and what it costs.

Doorman keeps its own temporary people (store.temp_people) and, separately, the
visits it has scheduled for them (store.temp_visits). A person is a saved
contact -- address, description, doors -- and holds no time at all; a visit is
one window, and holds no facts about the person. Scheduling somebody again is
therefore picking them and picking a length, not filling in a form.

A visit passes through three states:

    planned -> live -> ended

Whether a Kindoo user exists is a property of the visit, not the person. A seat
is consumed the moment that user is created and released when they are removed,
so creation is deliberately not the same act as writing the plan down: a window
starting next Tuesday holds no seat until Tuesday.

Times are stored and sent as UTC. They are *typed* in the site's local zone --
"the rest of today" means local midnight, not UTC midnight, and getting that
wrong would end someone's access six hours early.

Windows run in whole hours and always round up (see ceil_hour): asked for two
hours at 9:50, somebody gets in until 12:00. Every rounding in this module goes
the same way, because the cost of a few extra minutes of access is nothing next
to the cost of a person standing at a locked door being told they have a key.
"""
import datetime as dt
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import store
from .config import settings
from .kindoo import Kindoo, KindooError

log = logging.getLogger("doorman.temps")

UTC_FMT = "%Y-%m-%dT%H:%M:%S"
#: How Kindoo *accepts* an instant, which is not how it returns one. A write
#: takes local wall-clock time in ExpiryTimeZone with no zone marker at all,
#: and the server does the conversion to UTC itself; a read hands the same
#: value back as UTC with a Z. Sending what it returns earns a bare
#: "303 ServerError" with nothing to say which field was wrong.
API_FMT = "%Y-%m-%dT%H:%M:%S"

#: Quick windows, in the order they are offered. `None` means "work it out"
#: rather than "add this much to now".
#:
#: "A day" is the rest of today, not the next 24 hours: somebody let in for the
#: day is being let in for *this* day, and a key that quietly keeps working
#: until tomorrow morning is not what was meant.
PRESETS = [
    ("2h",    "2 hours",       dt.timedelta(hours=2)),
    ("today", "Today",         None),
    ("1w",    "1 week",        dt.timedelta(days=7)),
]


def zone():
    """The site's zone, falling back to UTC rather than failing to load.

    A mistyped zone name should make the times look wrong, not take the whole
    app down -- doors are more important than a correct offset.
    """
    try:
        return ZoneInfo(settings.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return dt.timezone.utc


def now_utc():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def to_utc_text(when):
    return when.astimezone(dt.timezone.utc).strftime(UTC_FMT)


def parse_utc(text):
    """Read a stored UTC stamp back. Empty or malformed reads as None so a bad
    row renders as 'no window' instead of raising on a list page."""
    if not text:
        return None
    try:
        return dt.datetime.strptime(text[:19], UTC_FMT).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def local_text(text, fmt="%a %-d %b, %-I:%M %p"):
    """A stored UTC stamp as the manager's clock shows it."""
    when = parse_utc(text)
    return when.astimezone(zone()).strftime(fmt) if when else ""


def api_text(text, early=None):
    """A stored UTC stamp as the local wall-clock time Kindoo wants on a write.

    The stamp goes in as the manager's own clock reads it -- 5pm is sent as
    17:00:00 -- paired with settings.expiry_timezone, which is the same zone
    named the way C# names it. The two must describe the same place: if
    site.timezone and kindoo.expiry_timezone ever disagree, every window
    silently lands hours out.

    `early` shifts it back, which is how the promised start becomes a start
    Kindoo will honour a little before the manager said (see LEAD).
    """
    when = parse_utc(text)
    if not when:
        return ""
    if early:
        when = when - early
    return when.astimezone(zone()).strftime(API_FMT)


class WindowError(ValueError):
    """A window that cannot be honoured -- shown to the manager as typed."""


def window(preset, day="", starts="", ends="", now=None):
    """(start, end) as UTC datetimes for one of the offered shapes.

    `preset` is a key from PRESETS, or 'day' for one whole local day, or
    'range' for a typed start and end. Everything typed is read in the site's
    local zone; everything returned is UTC.
    """
    tz = zone()
    now = (now or now_utc()).astimezone(tz)

    if preset == "day":
        d = _date(day, "Pick the day they need access.")
        start = dt.datetime.combine(d, dt.time(0, 0), tz)
        end = dt.datetime.combine(d + dt.timedelta(days=1), dt.time(0, 0), tz)
    elif preset == "range":
        start = _moment(starts, "Fill in when their access starts.")
        end = _moment(ends, "Fill in when their access ends.")
    elif preset == "today":
        start = now
        end = dt.datetime.combine(now.date() + dt.timedelta(days=1), dt.time(0, 0), tz)
    else:
        span = dict((k, td) for k, _, td in PRESETS).get(preset)
        if span is None:
            raise WindowError("Pick how long they need access for.")
        start, end = now, now + span

    end = ceil_hour(end)
    if end <= start:
        raise WindowError("That window ends before it starts.")
    # A window already over would create a user Kindoo expires immediately --
    # a seat spent on nothing. Refuse it while it is still just a typo.
    if end <= now:
        raise WindowError("That window has already passed.")
    return (start.astimezone(dt.timezone.utc).replace(microsecond=0),
            end.astimezone(dt.timezone.utc).replace(microsecond=0))


def ceil_hour(when):
    """Round a moment up to the top of the hour, in the site's own zone.

    Windows are quoted in whole hours: "two hours" asked at 9:50 runs to 12:00,
    not 11:50. Rounding up rather than to the nearest hour is deliberate and is
    the rule everywhere in this file -- every boundary errs towards the person
    still being able to get in, never towards them being shut out early.

    The step is taken as an absolute hour from the top of the current one, so a
    daylight-saving change produces a real instant rather than a wall-clock time
    that does not exist.
    """
    local = when.astimezone(zone())
    top = local.replace(minute=0, second=0, microsecond=0)
    if top == local:
        return when.astimezone(dt.timezone.utc)
    return top.astimezone(dt.timezone.utc) + dt.timedelta(hours=1)


def _date(text, complaint):
    try:
        return dt.date.fromisoformat((text or "").strip())
    except ValueError:
        raise WindowError(complaint) from None


def _moment(text, complaint):
    """Read an <input type=datetime-local> value in the site's zone."""
    raw = (text or "").strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(raw, fmt).replace(tzinfo=zone())
        except ValueError:
            continue
    raise WindowError(complaint)


def brought_forward(row, now=None):
    """The window a visit becomes if somebody is let in right now.

    Its **length** is what carries over, not its end. Keeping the end instead
    would turn a three-hour booking four days out into four days of access the
    moment anybody pressed "let in now" -- the opposite of what a tool built
    around a scarce seat should do when asked to be helpful.

    The new end rounds up to the hour like every other window here, and a visit
    that has already begun is left exactly as it is.
    """
    now = now or now_utc()
    start, end = parse_utc(row.get("starts_at")), parse_utc(row.get("ends_at"))
    if not start or not end or start <= now:
        return (start, end)
    return (now, ceil_hour(now + (end - start)))


def description_for(unit, description):
    """The text Kindoo will show, spelled the way the rest of the site spells it.

    Deliberately the same shape a permanent add produces: the unit name has to
    be in there or Doorman's own unit matching will not find this person again.
    """
    written = settings.write_name(unit) if unit else ""
    description = (description or "").strip()
    if written and description:
        return f"{written} ({description})"
    return written or description


def state_of(row, now=None):
    """What one visit actually is right now, as a (key, label) pair.

    The stored `status` records what Doorman *did*; the window says where we
    are in time. Both matter: a row still marked live whose end time has passed
    is waiting on Kindoo to expire it, which is not the same as one Doorman
    ended on purpose.
    """
    now = now or now_utc()
    status = (row.get("status") or "planned")
    start, end = parse_utc(row.get("starts_at")), parse_utc(row.get("ends_at"))

    if status == "cancelled":
        # Distinct from "ended" on purpose: nothing was ever created, nobody
        # was ever let in, and no seat was ever spent. Calling that "ended"
        # would put a non-event in the history looking like an event.
        return ("cancelled", "cancelled")
    if status == "failed":
        return ("failed", "not created")
    if status == "ended":
        return ("ended", "ended")
    if status == "live":
        if end and now > end:
            return ("expiring", "past end · in Kindoo")
        return ("live", "in Kindoo now")
    if start and now < start:
        return ("scheduled", "starts later")
    if end and now > end:
        return ("missed", "window passed")
    return ("due", "due now — not created")


#: States where somebody still holds, or is about to hold, a Kindoo seat.
HOLDS_SEAT = {"live", "expiring"}


def sort_key(row):
    """Live first, then what is coming, then what is over."""
    order = {"expiring": 0, "live": 1, "due": 2, "scheduled": 3,
             "failed": 4, "missed": 5, "ended": 6, "cancelled": 7}
    key, _ = state_of(row)
    return (order.get(key, 9), row.get("starts_at") or "", row.get("id") or 0)


# ---- becoming a real Kindoo user ------------------------------------------
# Creating the user is what spends a seat, so it happens as late as it can:
# immediately for a window that has already begun, and otherwise a few minutes
# before it does (see doorman/scheduler.py). Until then a temporary person
# exists only here, costing nothing.

#: How far ahead of the promised start everything happens: the Kindoo user is
#: created this early, AND Kindoo is told their access begins this early.
#:
#: Both halves matter. Creating early alone is not enough -- Kindoo enforces
#: StartAccessDoorsDate itself, so a user created at 4:50 for a 5:00 start
#: still cannot open a door at 4:59:30, and somebody told "you're in at five"
#: who pulls the handle as the clock turns would be left standing outside.
#: Giving the door gate the same margin means the promised time is the time
#: it certainly works by, not the time it might start working.
#:
#: Long enough to absorb a slow API call and a few minutes of clock drift
#: between us, Kindoo and the door controller; short enough that the seat is
#: not held for nothing and "early" is never meaningfully early.
LEAD = dt.timedelta(minutes=10)


def kindoo_for(owner):
    """A client acting as the manager who owns the row.

    Their own token, not a shared one: Kindoo attributes every action to the
    token holder, so a temporary person created for this manager must be
    created *by* them, whether the page or the scheduler is doing it.
    """
    account = store.get_account(owner)
    token = settings.token_for_account(account)
    if not token:
        return None
    return Kindoo(token, settings.eid, settings.base)


def activate(k, owner, row):
    """Create the Kindoo user for one visit. '' on success.

    `row` is a visit joined to its person, so the doors and description used
    are whatever that person's defaults say *now* -- editing someone's doors
    changes the next visit they are given, which is the point of saving them.

    The return value is a sentence for a manager to read, not an exception: a
    refusal here (no seats, address already in the site, a dead token) is
    ordinary news about one row, and the page or the scheduler carries on.
    """
    try:
        env = k.environment()
        cap = env.get("MaximumUsersLimitNow") or 0
        active = env.get("TotalActiveUsers") or 0
    except KindooError as e:
        return _failed(owner, row, f"Could not reach Kindoo: {e}")

    if cap and active >= cap:
        # Deliberately not marked failed: nothing is wrong with the row, the
        # site is simply full. It stays due and is tried again.
        return f"No free seats ({active}/{cap}) \u2014 remove someone first"

    try:
        if k.user_by_email(row["email"]):
            # Somebody with this address is already in the site. Inviting again
            # would either be refused or make a second record; neither is meant.
            return _failed(owner, row, f"{row['email']} is already in the site")

        k.invite_user(row["email"], description_for(row["unit"], row["description"]),
                      temp=True,
                      expiry=api_text(row["ends_at"]),
                      starts=api_text(row["starts_at"], early=LEAD),
                      timezone=settings.expiry_timezone)
        person = k.user_by_email(row["email"])
        if not person:
            return _failed(owner, row, "Invited, but they did not appear in Kindoo")
        if row["door_ids"]:
            k.grant_always_access(person["UserID"], row["door_ids"])
    except KindooError as e:
        return _failed(owner, row, f"Kindoo refused: {e}")

    store.update_temp_visit(owner, row["id"], status="live", note="",
                           kindoo_uid=str(person.get("UserID") or ""),
                           kindoo_euid=str(person.get("EUID") or ""))
    log.info("temp user %s created in Kindoo for %s until %s",
             row["email"], owner, row["ends_at"])
    return ""


def _failed(owner, row, message):
    """Record why a visit could not be created, and hand the same words back."""
    store.update_temp_visit(owner, row["id"], status="failed", note=message[:200])
    log.warning("temp user %s for %s failed: %s", row["email"], owner, message)
    return message


def end_now(k, owner, row):
    """Remove the Kindoo user early and close the visit. '' on success.

    The visit stays either way -- it is the history of who was let in, and the
    person stays whatever happens, ready to be scheduled again.
    """
    if row.get("kindoo_uid"):
        try:
            k.revoke_user(row["kindoo_uid"])
        except KindooError as e:
            # Already gone is the outcome we wanted; anything else is not.
            if not e.is_permission:
                return f"Could not remove them: {e}"
    store.update_temp_visit(owner, row["id"], status="ended", note="ended early",
                            ended_at=to_utc_text(now_utc()))
    return ""
