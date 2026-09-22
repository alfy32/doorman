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
    def invite_user(self, email, description, **kw):
        FakeKindoo.invites.append((email, description, kw))
        self.users_rows.append({"UserID": 99, "EUID": 999, "Username": email,
                                "DisplayName": "", "Description": description,
                                "HasAcceptedInvitation": False, "IsTempUser": True,
                                "InvitedOn": "2026-09-21T00:00:00Z"})
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
assert "Add someone to" in home and "New temporary user" in home, "home page content"
assert '<div class="brand"><a href="/"' in home, "brand links home"
assert "Add someone to" not in get("/ward"), "add form should have left the roster"
print("home page + brand link ok")

print("\n--- create a temp user for right now ---")
r = c.post("/temp", data={"email": "visitor@x.com", "name": "Pat Visitor",
                          "description": "Piano tuner", "preset": "2h"},
           follow_redirects=False)
print("  redirect:", r.headers["location"][:90])
assert FakeKindoo.invites, "should have been created in Kindoo immediately"
email, desc, kw = FakeKindoo.invites[-1]
print("  invited:", email, "|", desc)
print("  temp=", kw["temp"], "starts=", kw["starts"], "expiry=", kw["expiry"], "tz=", kw["timezone"])
assert kw["temp"] is True and kw["timezone"] == "Mountain Standard Time"
assert FakeKindoo.grants[-1] == (99, [6770]), FakeKindoo.grants
body = get("/temp")
assert "in Kindoo now" in body and "Pat Visitor" in body
print("  list shows them live, with the manager's default door")

print("\n--- schedule one for later: nothing sent to Kindoo yet ---")
before = len(FakeKindoo.invites)
later = (dt.datetime.now(temps.zone()) + dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
end = (dt.datetime.now(temps.zone()) + dt.timedelta(days=3, hours=4)).strftime("%Y-%m-%dT%H:%M")
r = c.post("/temp", data={"email": "later@x.com", "preset": "range",
                          "starts": later, "ends": end}, follow_redirects=False)
print("  redirect:", r.headers["location"][:90])
assert len(FakeKindoo.invites) == before, "must NOT take a seat three days early"
assert "starts later" in get("/temp")
print("  held as a plan, no seat taken")

print("\n--- the scheduler creates it when the window comes near ---")
from doorman import scheduler
row = [r for r in store.list_temp_users("me@x.com") if r["email"] == "later@x.com"][0]
soon = temps.parse_utc(row["starts_at"]) - dt.timedelta(minutes=5)   # inside the lead
print("  created:", scheduler.run_due(now=soon), "user(s)")
assert len(FakeKindoo.invites) == before + 1
email, desc, kw = FakeKindoo.invites[-1]
promised, sent = row["starts_at"], kw["starts"]
print(f"  promised start {promised}Z -> told Kindoo {sent}")
assert temps.parse_utc(sent[:19]) == temps.parse_utc(promised) - temps.LEAD, "10 min of margin"
assert scheduler.run_due(now=soon) == 0, "running twice must not create twice"
print("  idempotent on a second pass")

print("\n--- ending early gives the seat straight back ---")
row = [r for r in store.list_temp_users("me@x.com") if r["email"] == "visitor@x.com"][0]
c.post("/temp/end", data={"temp_id": row["id"]}, follow_redirects=False)
assert "99" in [str(x) for x in FakeKindoo.revoked], FakeKindoo.revoked
body = get("/temp")
assert "Ended access for visitor@x.com" in body or "ended" in body
print("  revoked in Kindoo, record kept:",
      [(r["email"], r["status"]) for r in store.list_temp_users("me@x.com")])

print("\n--- a window Kindoo already expired closes itself ---")
row = [r for r in store.list_temp_users("me@x.com") if r["email"] == "later@x.com"][0]
FakeKindoo.users_rows[:] = [u for u in FakeKindoo.users_rows if u["Username"] != "later@x.com"]
webapp._cache.clear()
get("/temp")
print("  after reconcile:", [(r["email"], r["status"], r["note"])
                             for r in store.list_temp_users("me@x.com")])
assert store.get_temp_user("me@x.com", row["id"])["status"] == "ended"

print("\n--- another manager sees none of this ---")
store.create_account("other@x.com", "Other", auth.hash_password("pw"))
store.update_account("other@x.com", token="tok2", unit=UNIT, door_ids=[6770])
c2 = TestClient(webapp.app)
c2.post("/login", data={"email": "other@x.com", "password": "pw"}, follow_redirects=False)
body = c2.get("/temp").text
assert "visitor@x.com" not in body and "later@x.com" not in body, "leaked another manager's list"
print("  other manager's page is empty:", "Nobody yet" in body)

print("\n--- bad input is refused, not crashed ---")
for data, why in [({"email": "x@x.com", "preset": "range", "starts": "", "ends": ""}, "no times"),
                  ({"email": "x@x.com", "preset": "day", "day": "2020-01-01"}, "past day"),
                  ({"email": "existing@x.com", "preset": "2h"}, "already in the site")]:
    r = c.post("/temp", data=data, follow_redirects=False)
    print(f"  {why:22} -> {r.headers['location'][:78]}")

print("\nALL OK")
