"""The temporary-user pages, end to end, against a stubbed Kindoo.

Run it directly -- there is no test runner to install:

    .venv/bin/python tests/test_temp_web.py

Kindoo is stubbed throughout. Nothing here touches the live API, and every
database write goes to a throwaway directory, so it is safe to run against a
working checkout.
"""
import datetime as dt, os, sys, tempfile

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
os.environ["DOORMAN_DATA_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("KINDOO_EID", "1")

from doorman import auth, store, temps

UNIT = "Northtown 1st Ward"

class FakeKindoo:
    """Just enough Kindoo to render every page and create a temp user."""
    invites, grants, revoked = [], [], []
    users_rows = [
        {"UserID": 11, "EUID": 111, "Username": "existing@x.com",
         "DisplayName": "Existing Person", "Description": f"{UNIT} (Clerk)",
         "HasAcceptedInvitation": True, "InvitedOn": "2026-01-02T00:00:00Z"},
    ]
    def __init__(self, *a, **kw): pass
    def environment(self): return {"MaximumUsersLimitNow": 232, "TotalActiveUsers": 230}
    def users(self, *a, **kw): return list(self.users_rows)
    def entry_points(self): return [{"ID": 6770, "Name": "AXTELL-EAST"},
                                    {"ID": 6769, "Name": "AXTELL-WEST"}]
    def access_logs(self, *a, **kw): return []
    def management_logs(self, *a, **kw): return []
    def access_permissions(self, uid): return []
    def user_by_email(self, email):
        for u in self.users_rows:
            if u["Username"].casefold() == (email or "").casefold(): return u
        return None
    next_uid = 99
    def invite_user(self, email, description, **kw):
        FakeKindoo.invites.append((email, description, kw))
        # A distinct id per invite, as the real API gives -- reusing one made
        # revoking somebody look like everybody had vanished from the roster.
        uid, FakeKindoo.next_uid = FakeKindoo.next_uid, FakeKindoo.next_uid + 1
        self.users_rows.append({"UserID": uid, "EUID": uid * 10, "Username": email,
                                "DisplayName": "", "Description": description,
                                "HasAcceptedInvitation": False, "IsTempUser": True,
                                "InvitedOn": "2026-09-21T00:00:00Z"})
        return None
    def grant_always_access(self, uid, ids): FakeKindoo.grants.append((uid, list(ids)))
    def revoke_user(self, uid):
        FakeKindoo.revoked.append(uid)
        self.users_rows[:] = [u for u in self.users_rows if str(u["UserID"]) != str(uid)]

import doorman.web.app as webapp
webapp.Kindoo = FakeKindoo
temps.Kindoo = FakeKindoo

# an account with a unit, doors and a token
store.create_account("me@x.com", "Me", auth.hash_password("pw"))
store.update_account("me@x.com", token="tok", unit=UNIT, door_ids=[6770])

from fastapi.testclient import TestClient
c = TestClient(webapp.app)
r = c.post("/login", data={"email": "me@x.com", "password": "pw"}, follow_redirects=False)
assert r.status_code == 303, r.status_code

def get(path, expect=200):
    r = c.get(path)
    assert r.status_code == expect, f"{path} -> {r.status_code}\n{r.text[:800]}"
    return r.text

print("--- every page renders ---")
for path in ["/", "/ward", "/temp", "/units", "/changes", "/settings",
             "/unit?name=Southtown%20Ward", "/person?uid=11", "/history?uid=11"]:
    body = get(path)
    print(f"  {path:34} {len(body):6d} bytes")

home = get("/")
assert "Let someone in temporarily" in home, "temp leads the home page"
assert "Add someone permanently" in home, "the permanent add is still there"
assert '<div class="brand"><a href="/"' in home, "brand links home"
assert "Add someone to" not in get("/ward"), "the add form should have left the roster"
print("home page leads with temporary access, brand links home")

print("\n--- someone new: saved as a person, and let in for 2 hours ---")
r = c.post("/temp/new", data={"email": "tuner@x.com", "name": "Pat Tuner",
                              "description": "Piano tuner", "preset": "2h",
                              "back": "/", "go": "now"}, follow_redirects=False)
print("  redirect:", r.headers["location"][:88])
assert FakeKindoo.invites, "a window starting now goes into Kindoo immediately"
email, desc, kw = FakeKindoo.invites[-1]
print("  invited:", email, "|", desc)
print("  temp=", kw["temp"], "starts=", kw["starts"], "expiry=", kw["expiry"], "tz=", kw["timezone"])
assert kw["temp"] is True and kw["timezone"] == "Mountain Standard Time"
assert not kw["expiry"].endswith("Z"), "a Z on a write is answered with 303 ServerError"
assert FakeKindoo.grants[-1] == (99, [6770]), FakeKindoo.grants
person = store.list_temp_people("me@x.com")[0]
assert person["door_ids"] == [6770], "doors default to the manager's own"
assert "starts_at" not in person, "the person holds no timeframe"
print("  person saved with doors", person["door_ids"], "-- and no timeframe on the person")

print("\n--- the common case: pick that person again, pick a length ---")
body = get("/temp")
# they are live, so their row offers the way out rather than the way in
assert "Pat Tuner" in body and "End access" in body
r = c.post("/temp/end", data={"visit_id": store.list_temp_visits("me@x.com")[0]["id"],
                              "back": "/"}, follow_redirects=False)
before = len(FakeKindoo.invites)
r = c.post("/temp/schedule", data={"person_id": person["id"], "preset": "today",
                                   "back": "/"}, follow_redirects=False)
print("  redirect:", r.headers["location"][:88])
assert len(FakeKindoo.invites) == before + 1, "scheduling an existing person re-invites them"
assert len(store.list_temp_people("me@x.com")) == 1, "and does not duplicate the person"
print("  visits so far:", [(v["email"], v["status"]) for v in store.list_temp_visits("me@x.com")])

print("\n--- editing their defaults changes the NEXT visit ---")
c.post("/temp/end", data={"visit_id": store.list_temp_visits("me@x.com")[0]["id"]},
       follow_redirects=False)
c.post("/temp/edit", data={"person_id": person["id"], "email": "tuner@x.com",
                           "name": "Pat Tuner", "description": "Organ tuner",
                           "door_ids": ["6769", "6782"]}, follow_redirects=False)
FakeKindoo.users_rows[:] = [u for u in FakeKindoo.users_rows if u["Username"] != "tuner@x.com"]
webapp._cache.clear()
c.post("/temp/schedule", data={"person_id": person["id"], "preset": "2h"},
       follow_redirects=False)
email, desc, kw = FakeKindoo.invites[-1]
print("  description now:", desc, "| doors now:", FakeKindoo.grants[-1][1])
assert "Organ tuner" in desc and sorted(FakeKindoo.grants[-1][1]) == [6769, 6782]

print("\n--- schedule one for later: nothing sent to Kindoo yet ---")
before = len(FakeKindoo.invites)
later = (dt.datetime.now(temps.zone()) + dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:00")
end = (dt.datetime.now(temps.zone()) + dt.timedelta(days=3, hours=4)).strftime("%Y-%m-%dT%H:00")
c.post("/temp/new", data={"email": "later@x.com", "preset": "range", "starts": later,
                          "ends": end, "go": "now"}, follow_redirects=False)
assert len(FakeKindoo.invites) == before, "must NOT take a seat three days early"
assert "booked for" in get("/temp")
print("  held as a plan, no seat taken")

print("\n--- the scheduler creates it when the window comes near ---")
from doorman import scheduler
visit = [v for v in store.list_temp_visits("me@x.com") if v["email"] == "later@x.com"][0]
soon = temps.parse_utc(visit["starts_at"]) - dt.timedelta(minutes=5)
print("  created:", scheduler.run_due(now=soon), "visit(s)")
assert len(FakeKindoo.invites) == before + 1
email, desc, kw = FakeKindoo.invites[-1]
want = (temps.parse_utc(visit["starts_at"]) - temps.LEAD).astimezone(temps.zone())
print(f"  promised {visit['starts_at']}Z -> told Kindoo {kw['starts']} ({temps.settings.expiry_timezone})")
assert kw["starts"] == want.strftime("%Y-%m-%dT%H:%M:%S"), (kw["starts"], want)
assert scheduler.run_due(now=soon) == 0, "running twice must not create twice"
print("  idempotent on a second pass")

print("\n--- \"let in now\" on a future booking moves the start, not just the seat ---")
# The bug this guards: creating the Kindoo user without moving the start spends
# a seat today and still refuses them at the door until Thursday.
booked = (dt.datetime.now(temps.zone()) + dt.timedelta(days=4)).strftime("%Y-%m-%dT%H:00")
booked_end = (dt.datetime.now(temps.zone()) + dt.timedelta(days=4, hours=3)).strftime("%Y-%m-%dT%H:00")
c.post("/temp/new", data={"email": "early@x.com", "name": "Early Bird", "preset": "range",
                          "starts": booked, "ends": booked_end, "go": "now"},
       follow_redirects=False)
v = [v for v in store.list_temp_visits("me@x.com") if v["email"] == "early@x.com"][0]
assert v["status"] == "planned", "a booking four days out must not be created yet"
was_end = v["ends_at"]
c.post("/temp/activate", data={"visit_id": v["id"]}, follow_redirects=False)
email, desc, kw = FakeKindoo.invites[-1]
v = store.get_temp_visit("me@x.com", v["id"])
sent_start = dt.datetime.strptime(kw["starts"], "%Y-%m-%dT%H:%M:%S")
now_local = dt.datetime.now(temps.zone()).replace(tzinfo=None)
print(f"  booked {booked} to {booked_end} (3 hours, four days out)")
print(f"  let in now -> Kindoo told start {kw['starts']}, expiry {kw['expiry']}")
assert sent_start <= now_local, "the door must open NOW, not on the original date"
assert v["status"] == "live"
# the LENGTH carries over, not the end: keeping the end would have handed out
# four days of access for a three-hour booking
assert v["ends_at"] != was_end, "the end must move with the start"
got = temps.parse_utc(v["ends_at"]) - temps.parse_utc(v["starts_at"])
print(f"  length kept: {got} (booked 3:00:00, rounded up to the hour)")
assert dt.timedelta(hours=3) <= got < dt.timedelta(hours=4), got
assert temps.parse_utc(v["ends_at"]) < temps.parse_utc(was_end), "must not over-grant"

print("\n--- but a failed future booking can go back in the queue instead ---")
store.update_temp_visit("me@x.com", v["id"], status="failed", note="pretend it broke",
                        starts_at=temps.to_utc_text(temps.now_utc() + dt.timedelta(days=2)))
before = len(FakeKindoo.invites)
c.post("/temp/requeue", data={"visit_id": v["id"]}, follow_redirects=False)
v = store.get_temp_visit("me@x.com", v["id"])
print("  status now:", v["status"], "| nothing sent to Kindoo:", len(FakeKindoo.invites) == before)
assert v["status"] == "planned" and len(FakeKindoo.invites) == before
store.update_temp_visit("me@x.com", v["id"], status="ended")

print("\n--- a future booking can be called off, with nothing to undo ---")
soon_s = (dt.datetime.now(temps.zone()) + dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:00")
soon_e = (dt.datetime.now(temps.zone()) + dt.timedelta(days=2, hours=2)).strftime("%Y-%m-%dT%H:00")
c.post("/temp/new", data={"email": "offagain@x.com", "name": "Off Again", "preset": "range",
                          "starts": soon_s, "ends": soon_e, "go": "now"}, follow_redirects=False)
booking = [x for x in store.list_temp_visits("me@x.com") if x["email"] == "offagain@x.com"][0]
# the button has to be on the page, not just the route -- it went missing once
page = get("/temp")
visits_block = page[page.index("Every visit"):]
assert '/temp/cancel' in visits_block, "every unopened visit needs a Cancel button"
assert visits_block.count('action="/temp/cancel"') >= 1
print("  Cancel is rendered in the visit list:",
      visits_block.count('action="/temp/cancel"'), "button(s)")
before = len(FakeKindoo.invites), len(FakeKindoo.revoked)
r = c.post("/temp/cancel", data={"visit_id": booking["id"]}, follow_redirects=False)
booking = store.get_temp_visit("me@x.com", booking["id"])
print("  redirect:", r.headers["location"][:80])
print("  status:", booking["status"], "|", booking["note"])
assert booking["status"] == "cancelled"
assert (len(FakeKindoo.invites), len(FakeKindoo.revoked)) == before, "Kindoo must not be touched"
assert temps.state_of(booking)[0] == "cancelled"
# and the scheduler must not pick it up when its time comes round
assert scheduler.run_due(now=temps.parse_utc(booking["starts_at"])) == 0, \
    "a cancelled booking must never be created"
print("  scheduler leaves it alone when its time comes")

print("\n--- but a visit already open must be ENDED, not cancelled ---")
live = [x for x in store.list_temp_visits("me@x.com") if x["status"] == "live"]
if live:
    r = c.post("/temp/cancel", data={"visit_id": live[0]["id"]}, follow_redirects=False)
    print("  refused ->", r.headers["location"][:88])
    assert "err=" in r.headers["location"]
    assert store.get_temp_visit("me@x.com", live[0]["id"])["status"] == "live"

print("\n--- a window Kindoo already expired closes itself ---")
FakeKindoo.users_rows[:] = [u for u in FakeKindoo.users_rows if u["Username"] != "later@x.com"]
webapp._cache.clear()
get("/temp")
v = [v for v in store.list_temp_visits("me@x.com") if v["email"] == "later@x.com"][0]
print("  after reconcile:", v["status"], "|", v["note"])
assert v["status"] == "ended"

print("\n--- another manager sees none of this ---")
store.create_account("other@x.com", "Other", auth.hash_password("pw"))
store.update_account("other@x.com", token="tok2", unit=UNIT, door_ids=[6770])
c2 = TestClient(webapp.app)
c2.post("/login", data={"email": "other@x.com", "password": "pw"}, follow_redirects=False)
body = c2.get("/temp").text
assert "tuner@x.com" not in body and "later@x.com" not in body, "leaked another manager's list"
print("  other manager's page is empty:", "Nobody saved yet" in body)

print("\n--- bad input is refused, not crashed ---")
for data, why in [({"person_id": person["id"], "preset": "range"}, "no times"),
                  ({"person_id": person["id"], "preset": "day", "day": "2020-01-01"}, "past day"),
                  ({"person_id": 99999, "preset": "2h"}, "someone else's person")]:
    r = c.post("/temp/schedule", data=data, follow_redirects=False)
    print(f"  {why:22} -> {r.headers['location'][:74]}")
r = c.post("/temp/schedule", data={"person_id": person["id"], "preset": "2h",
                                   "back": "https://evil.example/x"}, follow_redirects=False)
print(f"  {'offsite redirect':22} -> {r.headers['location'][:74]}")
assert r.headers["location"].startswith("/temp"), "must not bounce off-site"

print("\nALL OK")
