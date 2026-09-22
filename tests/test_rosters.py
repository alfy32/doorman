"""Who may act on which roster, and where the All units links point.

Run it directly -- there is no test runner to install:

    .venv/bin/python tests/test_rosters.py

Kindoo is stubbed throughout. Nothing here touches the live API, and every
database write goes to a throwaway directory.
"""
import os, sys, tempfile, urllib.parse
os.environ["DOORMAN_DATA_DIR"] = tempfile.mkdtemp(); os.environ["KINDOO_EID"] = "1"
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from doorman import auth, store
from doorman.config import settings
UNIT = (settings.units or ["Unit A"])[0]
OTHER = (settings.units or ["Unit A", "Unit B"])[1]

class FakeKindoo:
    rows = [
        {"UserID": 1, "EUID": 10, "Username": "mine@x.com", "DisplayName": "My Person",
         "Description": f"{UNIT} (Clerk)", "HasAcceptedInvitation": True},
        {"UserID": 2, "EUID": 20, "Username": "theirs@x.com", "DisplayName": "Their Person",
         "Description": f"{OTHER} (Clerk)", "HasAcceptedInvitation": True},
        {"UserID": 3, "EUID": 30, "Username": "orphan@x.com", "DisplayName": "No Unit Person",
         "Description": "Bulletin", "HasAcceptedInvitation": True},
        {"UserID": 4, "EUID": 40, "Username": "pending@x.com", "DisplayName": "",
         "Description": "Missionaries", "HasAcceptedInvitation": False,
         "InvitedOn": "2026-09-01T00:00:00Z"},
    ]
    revoked, resent = [], []
    def __init__(self, *a, **k): pass
    def environment(self): return {"MaximumUsersLimitNow": 232, "TotalActiveUsers": 230}
    def users(self, *a, **k): return list(self.rows)
    def entry_points(self): return [{"ID": 11, "Name": "NORTH DOOR"}]
    def access_logs(self, *a, **k): return []
    def user_by_email(self, e): return None
    def revoke_user(self, uid):
        FakeKindoo.revoked.append(str(uid))
        self.rows[:] = [u for u in self.rows if str(u["UserID"]) != str(uid)]
    def resend_invitation(self, uid, cc_manager=False): FakeKindoo.resent.append(str(uid))

import doorman.web.app as webapp
from doorman import temps
webapp.Kindoo = FakeKindoo; temps.Kindoo = FakeKindoo
store.create_account("me@x.com", "Me", auth.hash_password("pw"))
store.update_account("me@x.com", token="t", unit=UNIT, door_ids=[11])

from fastapi.testclient import TestClient
c = TestClient(webapp.app)
c.post("/login", data={"email": "me@x.com", "password": "pw"}, follow_redirects=False)

NO_UNIT = webapp.NO_UNIT
nounit_url = "/unit?name=" + urllib.parse.quote(NO_UNIT)

print("--- All units links to the right place for my own unit ---")
body = c.get("/units").text
assert 'href="/ward"' in body, "my own unit must link to the roster, not the let-in page"
row = body[body.index(UNIT) - 400: body.index(UNIT) + 80]
print("  my unit ->", "/ward" if 'href="/ward"' in row else "WRONG")
assert '"/unit?name=' in body, "other units still link to their read-only roster"
print("  other units -> /unit?name=...")

print("\n--- the no-unit roster is managed, other units are not ---")
mine_free = c.get(nounit_url).text
assert "No Unit Person" in mine_free and "Their Person" not in mine_free
assert 'action="/remove"' in mine_free, "no-unit people must be removable by anyone"
assert 'action="/resend"' in mine_free, "and resendable when they have not accepted"
assert "any manager can act here" in mine_free
print("  no-unit page: Remove + Resend present")

theirs = c.get("/unit?name=" + urllib.parse.quote(OTHER)).text
assert 'action="/remove"' not in theirs, "another unit's roster must stay read-only"
assert "Read-only" in theirs
print("  another unit's page: still read-only")

print("\n--- acting there returns there, not to my ward ---")
r = c.post("/remove", data={"uid": "3", "name": "No Unit Person", "back": NO_UNIT},
           follow_redirects=False)
print("  removed ->", r.headers["location"][:70])
assert r.headers["location"].startswith("/unit?name=")
assert "msg=" in r.headers["location"] and FakeKindoo.revoked == ["3"]

r = c.post("/resend", data={"uid": "4", "name": "", "back": NO_UNIT}, follow_redirects=False)
print("  resent  ->", r.headers["location"][:70])
assert r.headers["location"].startswith("/unit?name=") and FakeKindoo.resent == ["4"]

r = c.post("/remove", data={"uid": "1", "name": "My Person"}, follow_redirects=False)
print("  from my ward ->", r.headers["location"][:70])
assert r.headers["location"].startswith("/ward"), "the default is still the ward"

r = c.post("/remove", data={"uid": "2", "name": "x", "back": "https://evil.example"},
           follow_redirects=False)
print("  offsite back ->", r.headers["location"][:70])
assert r.headers["location"].startswith("/ward"), "must not bounce off-site"

r = c.post("/remove", data={"uid": "2", "name": "x", "back": "Not A Real Unit"},
           follow_redirects=False)
print("  unknown unit ->", r.headers["location"][:70])
assert r.headers["location"].startswith("/ward"), "an unknown roster falls back to the ward"

print("\nALL OK")
