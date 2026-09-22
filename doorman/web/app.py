"""Doorman — unit-manager tool.

Three jobs, in the order they matter:
  1. see the people in my unit, with the last time each opened a door
  2. add a person (email + calling) and give them our doors
  3. remove a person when the seat count gets tight

Everything else is secondary. The site-wide numbers exist only so a manager can
see whether another unit is eating the shared seat pool.
"""
import datetime as dt, logging, time
from collections import Counter
from pathlib import Path

import hashlib, os, secrets

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import access, auth, mail, store
from ..config import settings
from ..kindoo import Kindoo, KindooError
from ..units import match_unit_fuzzy

log = logging.getLogger("doorman.web")
app = FastAPI(title="Doorman")
# Session cookie key. Persisted so restarts don't sign everyone out; generated
# on first run and kept with the other local state.
_KEY_FILE = store.DB_PATH.parent / "session.key"
if not _KEY_FILE.exists():
    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _KEY_FILE.write_text(secrets.token_hex(32))
    os.chmod(_KEY_FILE, 0o600)
PUBLIC_PATHS = {"/login", "/signup", "/logout", "/set-password", "/reset"}
PUBLIC_PREFIXES = ("/static/",)          # the stylesheet must load on the login page too


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Everything needs a signed-in account except the sign-in screens."""
    path = request.url.path
    if path not in PUBLIC_PATHS and not path.startswith(PUBLIC_PREFIXES):
        email = request.session.get("email")
        if not email or not store.get_account(email):
            if not sign_in_via_access(request):
                request.session.clear()
                return RedirectResponse("/login", 303)
    return await call_next(request)


@app.middleware("http")
async def cache_policy(request: Request, call_next):
    """Cache only what is safe to cache.

    * /static is requested with a ?v=<content hash>, so a given URL can never
      change meaning -- it is cached hard, and an edit produces a new URL that
      is fetched immediately. No revalidation round-trip, no stale styling.
    * Everything else is live data about who can open doors. It is never
      cached, so a deploy or a change in Kindoo shows on the next load.
    """
    response = await call_next(request)
    if request.url.path.startswith(PUBLIC_PREFIXES):
        if request.query_params.get("v"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"      # revalidate
    else:
        response.headers["Cache-Control"] = "no-store"
    return response


# Added last so it wraps require_login: Starlette runs the most recently added
# middleware outermost, and the auth check needs request.session to exist.
# Secure cookie whenever the site is actually served over HTTPS, which the
# public_url tells us: a Secure cookie is never sent over plain HTTP, so
# setting it unconditionally would break a local http:// install.
app.add_middleware(SessionMiddleware, secret_key=_KEY_FILE.read_text().strip(),
                   session_cookie="doorman", max_age=60 * 60 * 24 * 30,
                   same_site="lax",
                   https_only=settings.public_url.startswith("https://"))
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def css_version():
    """Hash of the stylesheet, used to bust the browser cache on change.

    Recomputed per request (the file is small) so edits show up on reload
    without restarting; in exchange an unchanged file keeps its cached copy.
    """
    try:
        return hashlib.sha1((STATIC_DIR / "style.css").read_bytes()).hexdigest()[:10]
    except OSError:
        return "0"


templates.env.globals["css_version"] = css_version

NO_UNIT = "\u2014 no unit \u2014"
API_FMT = "%Y-%m-%dT%H:%M:%S"
LOG_WINDOW_DAYS = 89          # the API's maximum single query range
HISTORY_SLICE = 60            # logs are paged 60 days at a time


def slice_window(index, size=HISTORY_SLICE):
    """UTC bounds for the `index`-th slice going back in time.

    Slice 0 is the most recent `size` days. Each slice is its own request, which
    also sidesteps the API's 90-day ceiling however far back we page.
    """
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    end = now + dt.timedelta(days=1) if index == 0 else now - dt.timedelta(days=index * size)
    start = now - dt.timedelta(days=(index + 1) * size)
    return start.strftime(API_FMT), end.strftime(API_FMT)


def api_window(days):
    """(start, end) bounds for a log query, as the API wants them: UTC.

    The API takes naive datetime strings and reads them as UTC. Passing local
    time silently truncates the window by the UTC offset -- here that hid the
    six most recent hours of every log, which looked like missing history
    rather than a bug. The end gets a day of headroom so nothing recent is
    ever clipped.
    """
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return ((now - dt.timedelta(days=days)).strftime(API_FMT),
            (now + dt.timedelta(days=1)).strftime(API_FMT))
_cache, TTL = {}, 60


def me(request):
    return store.get_account(request.session.get("email")) or {}


def sign_in_via_access(request):
    """Sign in from a Cloudflare Access token, if one vouches for an address
    this site allows. Returns the account, or None to fall back to the form.

    An allow-listed address with no account yet gets one: Cloudflare has
    already proved they own it, which is exactly what sign-up would have
    established. They still choose their unit and paste their own Kindoo token
    on Settings.
    """
    email = access.verified_email(request, settings.access)
    if not email:
        return None
    acct = store.get_account(email)
    if not acct:
        if not auth.is_allowed(email, settings.allowed_emails):
            log.warning("Cloudflare Access vouched for %s, which is not in "
                        "allowed_emails -- refusing to create an account", email)
            return None
        acct = store.create_account(email)
        log.info("created an account for %s from a Cloudflare Access sign-in", email)
    request.session["email"] = acct["email"]
    return acct


def client(request):
    acct = me(request)
    tok = settings.token_for_account(acct)
    if not tok:
        raise KindooError("(no token)", 401,
                          "No Kindoo token saved for your account — add it on Settings.")
    return Kindoo(tok, settings.eid, settings.base)


def cached(request, key, fn, ttl=TTL):
    key = f"{me(request).get('email')}:{key}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def drop_cache(request):
    prefix = f"{me(request).get('email')}:"
    for k in list(_cache):
        if k.startswith(prefix):
            del _cache[k]


def last_seen_map(request, k):
    """UserID -> (last successful open, last DENIED attempt), over 89 days.

    Deliberately a second call: the user list carries no access history, so
    "when did this person last use their access" has to be joined from logs.

    Successes and failures are kept apart on purpose. The log records refused
    attempts too, and counting those as "used their access" is exactly
    backwards -- a denial means the person turned up and could NOT get in.
    """
    start, end = api_window(LOG_WINDOW_DAYS)
    try:
        rows = cached(request, "logs", lambda: k.access_logs(start, end))
    except KindooError:
        return {}
    ok, denied = {}, {}
    for r in rows:
        uid, ts = r.get("UserID"), r.get("Timestamp")
        if not uid or not ts:
            continue
        bucket = ok if r.get("Success") else denied
        if ts > bucket.get(uid, ""):
            bucket[uid] = ts
    return ok, denied


def my_people(users, unit):
    return [u for u in users
            if match_unit_fuzzy(u.get("Description") or "") == unit]


def _history(rows):
    """Turn consecutive revisions of one user into readable events.

    Each event carries `who` when the log names an actor. Kindoo records only
    two: who invited and who revoked. Nothing identifies who edited a
    description or made any other change, so `who` is None for those and the
    UI says so rather than leaving a silent gap.

    Careful with the first row: it is only the person's creation if its action
    is `Insert`. A membership older than the query window shows up with an
    ordinary `Update` first, and calling that "added to the site" reports the
    wrong creation date.
    """
    rows = sorted(rows, key=lambda r: (r.get("TimeStamp") or "", r.get("Revision") or 0))
    out, prev = [], None
    for r in rows:
        when, ev = r.get("TimeStamp") or "", []
        inviter = r.get("InvitedByUserName")
        revoker = r.get("RevokedByUserName")
        if r.get("Action") == "Insert":
            if prev is None:
                ev.append(("added", "Added to the site", inviter))
            else:
                ev.append(("added", "Re-invited"
                           + (" (after being removed)" if prev.get("RevokedOn") else ""),
                           inviter))
        if prev is not None:
            if not prev.get("AcceptedInvitation") and r.get("AcceptedInvitation"):
                # The only event whose actor is certain: the person themselves.
                ev.append(("accepted", "Accepted the invitation", None))
            if r.get("RevokedOn") and r.get("RevokedOn") != prev.get("RevokedOn"):
                ev.append(("removed", "Removed from the site", revoker))
            a, b = (prev.get("Description") or ""), (r.get("Description") or "")
            if a != b:
                ev.append(("edited", f"Description changed to {b!r}" if b
                                     else "Description cleared", None))
            if r.get("IsExpired") and not prev.get("IsExpired"):
                ev.append(("removed", "Expired", None))
        if not ev:
            ev.append(("edited", "Data update", None))
        for kind, text, who in ev:
            out.append({"when": when, "kind": kind, "text": text, "who": who})
        prev = r
    out.reverse()
    return out


def _person_from_log(rows):
    """A stand-in user record for somebody no longer in the roster.

    Only the audit log remembers them, so the fields come from its most recent
    revision; `InvitedBy` is faked into the same shape `_inviter` expects.
    """
    last = sorted(rows, key=lambda r: (r.get("TimeStamp") or "",
                                       r.get("Revision") or 0))[-1]
    return {
        "UserID": last.get("UserID"), "EUID": last.get("EUID"),
        "Username": last.get("Username") or "",
        "DisplayName": (last.get("FullName") or last.get("Username") or "").strip(),
        "Description": last.get("Description") or "",
        "InvitedOn": last.get("InvitedOn"),
        "InvitedBy": {"Username": last.get("InvitedByUserName") or "",
                      "DisplayName": " ".join(filter(None, [
                          last.get("InvitedByFirstName"),
                          last.get("InvitedByLastName")])).strip()},
        "RevokedOn": last.get("RevokedOn"),
        "RevokedByUserName": last.get("RevokedByUserName"),
    }


def _inviter(u):
    """Who put this person in the site. `InvitedBy` is a whole nested user
    object; we only want a name and an address out of it."""
    b = u.get("InvitedBy") or {}
    if not isinstance(b, dict) or not b:
        return None
    email = (b.get("Username") or "").strip()
    name = (b.get("DisplayName")
            or f"{b.get('FirstName','')} {b.get('LastName','')}".strip()).strip()
    return {"name": name or email or "someone",
            "email": email,
            # Most sites have a service account that creates users in bulk.
            # Naming it as an automated sync reads better than showing an
            # address nobody recognises. Configured, never hardcoded.
            "automated": settings.is_automated(email)}


def _days_since(ts):
    if not ts:
        return None
    try:
        return (dt.datetime.now(dt.timezone.utc)
                - dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))).days
    except ValueError:
        return None


def _people_rows(users, last, unit, denied=None):
    return _rows_for(my_people(users, unit), last, denied)


def _rows_for(people, last, denied=None):
    rows = []
    for u in people:
        ts = last.get(u.get("UserID"))
        dts = (denied or {}).get(u.get("UserID"))
        # Kindoo has no real name until a person accepts, and puts their email
        # in DisplayName meanwhile. Track that so the table can keep its columns
        # aligned instead of showing the address twice.
        raw_name = (u.get("DisplayName") or "").strip()
        email = (u.get("Username") or "").strip()
        has_name = bool(raw_name) and raw_name.casefold() != email.casefold()
        rows.append({
            "uid": u.get("UserID"), "euid": u.get("EUID"),
            "name": raw_name or email or "?",
            "has_name": has_name,
            "email": email,
            "desc": u.get("Description") or "",
            "accepted": bool(u.get("HasAcceptedInvitation")),
            "invited_on": (u.get("InvitedOn") or "")[:10],
            # Kindoo's own app hides its Resend button when this is false, but the
            # API honours the call regardless -- verified against a real account
            # whose flag was false. So it is a hint, not a rule: we offer Resend
            # to anyone who still has to accept.
            "can_resend": bool(u.get("CanResendRegistrationEmail")),
            "temp": bool(u.get("IsTempUser")),
            "expiry": u.get("ExpiryDateAtTimeZone"),
            "last": ts, "days": _days_since(ts),
            "denied": dts, "denied_days": _days_since(dts),
        })
    rows.sort(key=lambda r: (r["last"] or ""), reverse=True)
    return rows


@app.get("/", response_class=HTMLResponse)
def index(request: Request, msg: str = "", err: str = ""):
    user = me(request)
    unit = user.get("unit") or ""
    try:
        k = client(request)
        env = cached(request, "env", k.environment)
        users = cached(request, "users", k.users)
        last, denied = last_seen_map(request, k)
        doors = cached(request, "doors", k.entry_points, ttl=600)
    except KindooError as e:
        return templates.TemplateResponse(request, "error.html",
                                          {"user": user, "err": e}, status_code=503)

    rows = _people_rows(users, last, unit, denied)
    my_doors = [d for d in doors if d.get("ID") in set(user.get("door_ids") or [])]
    alloc = settings.allocation_for(unit) or 0
    stale = [r for r in rows if r["days"] is None or r["days"] > 60]
    turned_away = [r for r in rows if not r["last"] and r["denied"]]
    return templates.TemplateResponse(request, "ward.html", {
        "user": user, "unit": unit, "rows": rows, "alloc": alloc,
        "turned_away": turned_away,
        "doors": sorted(doors, key=lambda d: d.get("Name") or ""), "my_doors": my_doors,
        "used": len(rows), "stale": stale,
        "cap": env.get("MaximumUsersLimitNow") or 0,
        "active": env.get("TotalActiveUsers") or 0,
        "window": LOG_WINDOW_DAYS, "msg": msg, "err": err,
    })


@app.post("/add")
def add_person(request: Request, email: str = Form(...), calling: str = Form(""),
               unit: str = Form(""), door_ids: list[str] = Form(default=[])):
    """Invite a person, then give them this unit's doors."""
    user = me(request)
    unit = unit or user.get("unit") or ""
    email = (email or "").strip()
    calling = (calling or "").strip()
    # Write the unit exactly as the rest of the site spells it, doubled
    # spaces and all -- see settings.unit_write_names.
    written = settings.write_name(unit)
    desc = f"{written} ({calling})" if calling else written
    k = client(request)
    try:
        if k.user_by_email(email):
            return RedirectResponse(f"/?err={email} is already in the site", 303)
        k.invite_user(email, desc)
        drop_cache(request)
        person = k.user_by_email(email)
        if not person:
            return RedirectResponse(
                "/?err=Invited, but the new user did not appear — check Kindoo", 303)
        doors = [int(d) for d in door_ids if d.strip()] or (user.get("door_ids") or [])
        if doors:
            k.grant_always_access(person["UserID"], doors)
        drop_cache(request)
        return RedirectResponse(f"/?msg=Added {email} with {len(doors)} doors", 303)
    except KindooError as e:
        return RedirectResponse(f"/?err={e}", 303)


@app.post("/remove")
def remove_person(request: Request, uid: str = Form(...), name: str = Form("")):
    try:
        client(request).revoke_user(uid)
        drop_cache(request)
        return RedirectResponse(f"/?msg=Removed {name or uid}", 303)
    except KindooError as e:
        return RedirectResponse(f"/?err={e}", 303)


@app.get("/person", response_class=HTMLResponse)
def person_page(request: Request, uid: str = "", msg: str = "", err: str = "",
                ddays: int = HISTORY_SLICE):
    """One person: their door history, and what they can actually open.

    Mirrors Kindoo's own per-user Logs view. Door permissions need their own
    call -- the user list never populates AccessPermissions.
    """
    user = me(request)
    ddays = max(HISTORY_SLICE, min(int(ddays or HISTORY_SLICE), 3650))
    dslices = -(-ddays // HISTORY_SLICE)         # ceil
    try:
        k = client(request)
        users = cached(request, "users", k.users)
        person = next((u for u in users if u.get("UserID") == uid), None)
        if not person:
            # They have been removed -- there are no doors or current access to
            # show, but their history still exists and is what you came for.
            return RedirectResponse(f"/history?uid={uid}", 303)
        # Door activity pages the same way as the account history, a month per
        # click, each slice cached on its own.
        alogs = []
        for i in range(dslices):
            a, b = slice_window(i)
            alogs += cached(request, f"alog{i}", lambda a=a, b=b: k.access_logs(a, b))
        seen_l, mine = set(), []
        for r in alogs:
            if r.get("UserID") != uid or r.get("ID") in seen_l:
                continue
            seen_l.add(r.get("ID"))
            mine.append(r)
        mine.sort(key=lambda r: r.get("Timestamp") or "", reverse=True)
        perms = k.access_permissions(uid)
        all_doors = sorted(cached(request, "doors", k.entry_points, ttl=600),
                           key=lambda d: d.get("Name") or "")
        doors = {d["ID"]: d.get("Name") for d in all_doors}
        current_ids = {p.get("DoorID") for p in perms if p.get("DoorID")}
        can_open = sorted(doors.get(i, str(i)) for i in current_ids)
        # We only manage plain "always" access. Anyone on a schedule or a named
        # rule is left alone rather than silently flattened.
        scheduled = [p for p in perms if not p.get("IsAlways")]
    except KindooError as e:
        return templates.TemplateResponse(request, "error.html",
                                          {"user": user, "err": e}, status_code=503)
    ok = [r for r in mine if r.get("Success")]
    return templates.TemplateResponse(request, "person.html", {
        "user": user, "p": person, "rows": mine[:200], "count": len(mine),
        "ok_count": len(ok), "denied_count": len(mine) - len(ok),
        "can_open": can_open, "window": LOG_WINDOW_DAYS,
        "all_doors": all_doors, "current_ids": current_ids,
        "scheduled": scheduled,
        "accepted": bool(person.get("HasAcceptedInvitation")),
        "invited_by": _inviter(person), "invited_on": person.get("InvitedOn"),
        "slice": HISTORY_SLICE, "ddays": dslices * HISTORY_SLICE,
        "more_ddays": (dslices + 1) * HISTORY_SLICE,
        "msg": msg, "err": err,
        "name": (person.get("DisplayName") or person.get("Username") or "?").strip(),
    })


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request, uid: str = "", hdays: int = HISTORY_SLICE):
    """One person's membership audit trail, paged backwards from today."""
    user = me(request)
    hdays = max(HISTORY_SLICE, min(int(hdays or HISTORY_SLICE), 3650))
    slices = -(-hdays // HISTORY_SLICE)
    try:
        k = client(request)
        users = cached(request, "users", k.users)
        person = next((u for u in users if u.get("UserID") == uid), None)
        rows = []
        for i in range(slices):
            a, b = slice_window(i)
            rows += cached(request, f"mlog{i}", lambda a=a, b=b: k.management_logs(a, b))
    except KindooError as e:
        return templates.TemplateResponse(request, "error.html",
                                          {"user": user, "err": e}, status_code=503)
    seen, mine = set(), []
    for r in rows:
        key = (r.get("Revision"), r.get("TimeStamp"))
        if r.get("UserID") == uid and key not in seen:
            seen.add(key)
            mine.append(r)
    gone = person is None
    if gone:
        # Removed from the site: rebuild what we can from the audit trail so the
        # page still answers "who was this and what happened to them".
        if not mine:
            return RedirectResponse("/?err=No record of that person", 303)
        person = _person_from_log(mine)
    return templates.TemplateResponse(request, "history.html", {
        "user": user, "p": person, "history": _history(mine), "gone": gone,
        "name": (person.get("DisplayName") or person.get("Username") or "?").strip(),
        "invited_by": _inviter(person), "invited_on": person.get("InvitedOn"),
        "hdays": slices * HISTORY_SLICE, "slice": HISTORY_SLICE,
        "more_days": (slices + 1) * HISTORY_SLICE})


@app.post("/person/doors")
def save_person_doors(request: Request, uid: str = Form(...), euid: str = Form(...),
                      name: str = Form(""), door_ids: list[str] = Form(default=[])):
    """Set exactly which doors this person can always open.

    Diffed rather than reset-and-reapply: granting is additive and revoking is
    by door, so touching only what changed avoids a window where someone has no
    access at all.
    """
    want = {int(d) for d in door_ids if d.strip()}
    k = client(request)
    try:
        perms = k.access_permissions(uid)
        have = {p.get("DoorID") for p in perms if p.get("DoorID")}
        add, remove = want - have, have - want
        if add:
            k.grant_always_access(uid, sorted(add))
        # Revoking is per access-right record, one call each -- not by door id.
        for p in perms:
            if p.get("DoorID") in remove and p.get("ID") is not None:
                k.revoke_access_right(p["ID"])
        drop_cache(request)
        if not add and not remove:
            note = f"No door changes for {name}"
        else:
            bits = []
            if add:    bits.append(f"added {len(add)}")
            if remove: bits.append(f"removed {len(remove)}")
            note = f"{name}: {' and '.join(bits)} door(s)"
        return RedirectResponse(f"/person?uid={uid}&msg={note}", 303)
    except KindooError as e:
        return RedirectResponse(f"/person?uid={uid}&err=Could not change doors: {e}", 303)


@app.post("/resend")
def resend_invite(request: Request, uid: str = Form(...), name: str = Form(""),
                  cc: str = Form("")):
    """Ask Kindoo to send the invitation email again.

    Uses Kindoo's own resend endpoint, so it cannot create a second user the
    way re-running the invite call might.
    """
    try:
        client(request).resend_invitation(uid, cc_manager=bool(cc))
        return RedirectResponse(f"/?msg=Invitation re-sent to {name or uid}", 303)
    except KindooError as e:
        if e.is_permission:
            return RedirectResponse(
                f"/?err=Kindoo refused to re-send to {name or uid} "
                f"(NoPermission) — they may no longer be in the site", 303)
        return RedirectResponse(f"/?err=Could not re-send to {name or uid}: {e}", 303)


@app.get("/unit", response_class=HTMLResponse)
def unit_page(request: Request, name: str = ""):
    """The people in any unit. Read-only: another unit's roster is its own
    manager's business, so there is no add or remove here -- only your own unit
    (the front page) is managed."""
    user = me(request)
    if name and name == (user.get("unit") or ""):
        return RedirectResponse("/", 303)          # own unit = the managed view
    try:
        k = client(request)
        users = cached(request, "users", k.users)
        last, denied = last_seen_map(request, k)
    except KindooError as e:
        return templates.TemplateResponse(request, "error.html",
                                          {"user": user, "err": e}, status_code=503)
    if name == NO_UNIT:
        people = [u for u in users
                  if match_unit_fuzzy(u.get("Description") or "") is None]
    else:
        people = [u for u in users
                  if match_unit_fuzzy(u.get("Description") or "") == name]
    rows = _rows_for(people, last, denied)
    return templates.TemplateResponse(request, "unit.html", {
        "user": user, "unit": name, "rows": rows, "window": LOG_WINDOW_DAYS,
        "alloc": settings.allocation_for(name), "idle": sum(1 for r in rows if not r["last"])})


@app.get("/changes", response_class=HTMLResponse)
def changes_page(request: Request, days: int = HISTORY_SLICE, scope: str = "mine"):
    """Everything that has happened to accounts lately, newest first.

    The audit log stores revisions per user, so events only exist once
    consecutive revisions of the SAME user are diffed. This groups by user,
    derives each one's events, then merges them into a single timeline.
    """
    user = me(request)
    days = max(HISTORY_SLICE, min(int(days or HISTORY_SLICE), 3650))
    slices = -(-days // HISTORY_SLICE)
    try:
        k = client(request)
        users = cached(request, "users", k.users)
        rows = []
        for i in range(slices):
            a, b = slice_window(i)
            rows += cached(request, f"mlog{i}", lambda a=a, b=b: k.management_logs(a, b))
    except KindooError as e:
        return templates.TemplateResponse(request, "error.html",
                                          {"user": user, "err": e}, status_code=503)

    by_uid, seen = {}, set()
    for r in rows:
        uid, key = r.get("UserID"), (r.get("UserID"), r.get("Revision"), r.get("TimeStamp"))
        if not uid or key in seen:
            continue
        seen.add(key)
        by_uid.setdefault(uid, []).append(r)

    known = {u.get("UserID"): u for u in users}
    my_unit = user.get("unit") or ""
    feed = []
    for uid, urows in by_uid.items():
        u = known.get(uid)
        desc = (u or urows[-1]).get("Description") or ""
        unit = match_unit_fuzzy(desc)
        if scope == "mine" and my_unit and unit != my_unit:
            continue
        raw_name = ((u or {}).get("DisplayName")
                    or urows[-1].get("FullName") or "").strip()
        email = ((u or {}).get("Username") or urows[-1].get("Username") or "").strip()
        has_name = bool(raw_name) and raw_name.casefold() != email.casefold()
        for ev in _history(urows):
            ev.update({"uid": uid, "name": raw_name or email or "?",
                       "has_name": has_name, "email": email, "unit": unit,
                       "gone": u is None})
            feed.append(ev)
    feed.sort(key=lambda e: e["when"], reverse=True)
    return templates.TemplateResponse(request, "changes.html", {
        "user": user, "feed": feed[:400], "count": len(feed), "days": days,
        "slice": HISTORY_SLICE, "more_days": (slices + 1) * HISTORY_SLICE,
        "scope": scope, "my_unit": my_unit})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = ""):
    user = me(request)
    try:
        doors = cached(request, "doors", client(request).entry_points, ttl=600)
    except KindooError as e:
        return templates.TemplateResponse(request, "error.html",
                                          {"user": user, "err": e}, status_code=503)
    chosen = set(user.get("door_ids") or [])
    return templates.TemplateResponse(request, "settings.html", {
        "user": user, "doors": sorted(doors, key=lambda d: d.get("Name") or ""),
        "chosen": chosen, "msg": msg, "units": settings.units,
        "alloc": settings.seat_allocation})


@app.post("/settings")
def save_settings(request: Request, door_ids: list[str] = Form(default=[]),
                  unit: str = Form(""), token: str = Form(""), name: str = Form("")):
    """Persist this manager's own settings to the database."""
    email = me(request).get("email")
    unit = unit.strip()
    if unit and unit not in settings.units:       # only a configured unit
        unit = ""
    fields = {"door_ids": [int(d) for d in door_ids if d.strip()],
              "name": name.strip()}
    if unit:
        fields["unit"] = unit
    if token.strip():                      # blank means "leave it alone"
        fields["token"] = token.strip()
    settings.update_account(email, **fields)
    drop_cache(request)
    return RedirectResponse("/settings?msg=Saved", 303)


@app.get("/units", response_class=HTMLResponse)
def units_page(request: Request, sort: str = "people"):
    """Who is using the shared seat pool, by unit."""
    user = me(request)
    try:
        k = client(request)
        env = cached(request, "env", k.environment)
        users = cached(request, "users", k.users)
        last, denied = last_seen_map(request, k)
    except KindooError as e:
        return templates.TemplateResponse(request, "error.html",
                                          {"user": user, "err": e}, status_code=503)

    counts, idle = Counter(), Counter()
    for u in users:
        unit = match_unit_fuzzy(u.get("Description") or "") or NO_UNIT
        counts[unit] += 1
        if not last.get(u.get("UserID")):
            idle[unit] += 1

    rows = []
    for unit, n in counts.items():
        alloc = settings.allocation_for(unit)
        rows.append({
            "unit": unit, "n": n, "idle": idle[unit], "active": n - idle[unit],
            "alloc": alloc,
            "over": (alloc or 0) and n > alloc,
            "diff": (n - alloc) if alloc else None,
            "mine": unit == (user.get("unit") or ""),
            "measured": bool(alloc),
        })
    key = {"idle": lambda r: (r["idle"], r["n"]),
           "over": lambda r: ((r["diff"] if r["diff"] is not None else -999), r["n"]),
           }.get(sort, lambda r: (r["n"], r["idle"]))
    rows.sort(key=key, reverse=True)
    mx = max([r["n"] for r in rows] or [1])
    return templates.TemplateResponse(request, "units.html", {
        "user": user, "rows": rows, "window": LOG_WINDOW_DAYS, "sort": sort, "mx": mx,
        "cap": env.get("MaximumUsersLimitNow") or 0,
        "site_active": env.get("TotalActiveUsers") or 0, "total": len(users),
        "total_idle": sum(r["idle"] for r in rows), "no_unit": NO_UNIT})


# ---------------------------------------------------------------- auth ----

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, msg: str = "", err: str = ""):
    if request.session.get("email"):
        return RedirectResponse("/", 303)
    if sign_in_via_access(request):        # one login, not two
        return RedirectResponse("/", 303)
    return templates.TemplateResponse(request, "login.html",
                                      {"msg": msg, "err": err, "user": None})


@app.post("/login")
def do_login(request: Request, email: str = Form(...), password: str = Form(...)):
    acct = store.get_account(auth.normalise(email))
    if not acct or not acct.get("password_hash") or \
            not auth.verify_password(password, acct["password_hash"]):
        # Deliberately vague: don't reveal which addresses exist.
        return templates.TemplateResponse(
            request, "login.html",
            {"err": "That email and password don't match.", "user": None},
            status_code=401)
    request.session["email"] = acct["email"]
    return RedirectResponse("/", 303)


def _send_link(request, email, purpose, subject, intro):
    """Email a single-use link proving control of `email`. Returns an error
    string, or None on success."""
    token = auth.issue_token(email, purpose)
    base = settings.public_url or str(request.base_url).rstrip("/")
    link = f"{base}/{'set-password' if purpose == 'signup' else 'reset'}?token={token}"
    hours = auth.SIGNUP_HOURS if purpose == "signup" else auth.RESET_HOURS
    body = (f"{intro}\n\n{link}\n\n"
            f"The link works once and expires in {hours} hours.\n"
            f"If you weren't expecting this, ignore it — nothing has changed.\n")
    try:
        if mail.send(settings.smtp, email, subject, body):
            return None
    except mail.MailError as e:
        log.warning("mail failed: %s", e)
        return "Could not send the email. Tell whoever runs this server."
    # No SMTP configured: the link is in the server log, not delivered.
    log.warning("NO SMTP — %s link for %s: %s", purpose, email, link)
    return ("Email isn't set up on this server yet, so the link couldn't be "
            "sent. Whoever runs it can find the link in the server log.")


@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request, err: str = "", sent: str = ""):
    return templates.TemplateResponse(request, "signup.html",
                                      {"err": err, "sent": sent, "user": None})


@app.post("/signup")
def do_signup(request: Request, email: str = Form(...)):
    """Step one: ask for a link. Never reveals whether the address is allowed
    or already registered -- the reply is the same either way."""
    email = auth.normalise(email)
    if auth.is_allowed(email, settings.allowed_emails):
        acct = store.get_account(email)
        if acct and acct.get("password_hash"):
            problem = _send_link(request, email, "reset",
                                 "Doorman password reset",
                                 "You already have a Doorman account. "
                                 "To set a new password, open this link:")
        else:
            problem = _send_link(request, email, "signup",
                                 "Set up your Doorman account",
                                 "You've been given access to Doorman. "
                                 "Set your password here:")
        if problem:
            log.error("signup link for %s: %s", email, problem)
    # Identical reply whichever branch ran -- including when sending failed.
    # Anything else would let a stranger probe which addresses are allowed.
    return RedirectResponse("/signup?sent=1", 303)


@app.get("/set-password", response_class=HTMLResponse)
def set_password_form(request: Request, token: str = "", err: str = ""):
    email, why = auth.check_token(token, "signup")
    if why:
        return templates.TemplateResponse(request, "setpassword.html",
                                          {"fatal": why, "user": None}, status_code=400)
    return templates.TemplateResponse(request, "setpassword.html", {
        "token": token, "email": email, "err": err, "user": None,
        "heading": "Set your password", "purpose": "signup"})


@app.post("/set-password")
def set_password(request: Request, token: str = Form(...), name: str = Form(""),
                 password: str = Form(...), confirm: str = Form("")):
    email, why = auth.check_token(token, "signup")
    if why:
        return templates.TemplateResponse(request, "setpassword.html",
                                          {"fatal": why, "user": None}, status_code=400)
    problem = auth.password_problem(password, confirm)
    if problem:
        return templates.TemplateResponse(request, "setpassword.html", {
            "token": token, "email": email, "err": problem, "user": None,
            "heading": "Set your password", "purpose": "signup"}, status_code=400)
    if not store.get_account(email):
        store.create_account(email, name.strip())
    store.update_account(email, password_hash=auth.hash_password(password),
                         **({"name": name.strip()} if name.strip() else {}))
    auth.consume_token(token)
    request.session["email"] = email
    return RedirectResponse("/settings?msg=Welcome — add your Kindoo token below", 303)


@app.get("/reset", response_class=HTMLResponse)
def reset_form(request: Request, token: str = "", err: str = ""):
    email, why = auth.check_token(token, "reset")
    if why:
        return templates.TemplateResponse(request, "setpassword.html",
                                          {"fatal": why, "user": None}, status_code=400)
    return templates.TemplateResponse(request, "setpassword.html", {
        "token": token, "email": email, "err": err, "user": None,
        "heading": "Choose a new password", "purpose": "reset"})


@app.post("/reset")
def do_reset(request: Request, token: str = Form(...),
             password: str = Form(...), confirm: str = Form("")):
    email, why = auth.check_token(token, "reset")
    if why:
        return templates.TemplateResponse(request, "setpassword.html",
                                          {"fatal": why, "user": None}, status_code=400)
    problem = auth.password_problem(password, confirm)
    if problem:
        return templates.TemplateResponse(request, "setpassword.html", {
            "token": token, "email": email, "err": problem, "user": None,
            "heading": "Choose a new password", "purpose": "reset"}, status_code=400)
    store.update_account(email, password_hash=auth.hash_password(password))
    auth.consume_token(token)
    request.session["email"] = email
    return RedirectResponse("/?msg=Password changed", 303)


@app.get("/refresh")
def refresh(request: Request, next: str = "/"):
    """Drop this manager's cached Kindoo responses and reload.

    Our own writes clear the cache already; this is for changes made elsewhere
    -- in Kindoo directly, or by an automated sync.
    """
    drop_cache(request)
    return RedirectResponse(next if next.startswith("/") else "/", 303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    # Clearing only the local session would be theatre: the Access token is
    # still in the browser, so the next request would sign them straight back
    # in. Send them to Cloudflare to drop it.
    home = settings.public_url or str(request.base_url).rstrip("/")
    away = access.logout_url(settings.access, return_to=home + "/")
    return RedirectResponse(away or "/login?msg=Signed out", 303)
