"""The person page: where its back link goes, and how times are shown.

Run it directly -- there is no test runner to install:

    .venv/bin/python tests/test_person.py

Kindoo is stubbed, with the ids it really uses (GUID strings, not integers --
an earlier stub used integers and the lookup silently missed every time).
Nothing here touches the live API.
"""
import os, re, sys, tempfile, urllib.parse
os.environ["DOORMAN_DATA_DIR"] = tempfile.mkdtemp(); os.environ["KINDOO_EID"] = "1"
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from doorman import auth, store, temps
from doorman.config import settings
UNIT = (settings.units or ["Unit A"])[0]

UID = "916a4ce0-0000-0000-0000-000000000003"

class FakeKindoo:
    rows = [{"UserID": UID, "EUID": "euid-30", "Username": "orphan@x.com",
             "DisplayName": "No Unit Person", "Description": "Bulletin",
             "HasAcceptedInvitation": False, "InvitedOn": "2026-05-14T00:50:26Z"}]
    logs = [{"ID": "log-1", "UserID": UID, "Timestamp": "2026-08-26T22:04:11Z", "DoorName": "NORTH DOOR",
             "Success": False, "ReasonText": "NoAccessRights"},
            {"ID": "log-2", "UserID": UID, "Timestamp": "2026-09-21T21:37:20Z", "DoorName": "SOUTH DOOR",
             "Success": True}]
    def __init__(self, *a, **k): pass
    def environment(self): return {"MaximumUsersLimitNow": 232, "TotalActiveUsers": 230}
    def users(self, *a, **k): return list(self.rows)
    def entry_points(self): return [{"ID": 11, "Name": "NORTH DOOR"}]
    def access_logs(self, *a, **k): return list(self.logs)
    def access_permissions(self, uid): return []
    def resend_invitation(self, *a, **k): pass
    def management_logs(self, *a, **k): return []
    def user_by_email(self, e): return None

import doorman.web.app as webapp
webapp.Kindoo = FakeKindoo; temps.Kindoo = FakeKindoo
store.create_account("me@x.com", "Me", auth.hash_password("pw"))
store.update_account("me@x.com", token="t", unit=UNIT, door_ids=[11])
from fastapi.testclient import TestClient
c = TestClient(webapp.app)
c.post("/login", data={"email": "me@x.com", "password": "pw"}, follow_redirects=False)

NO_UNIT = webapp.NO_UNIT
url = "/person?uid=" + UID + "&back=" + urllib.parse.quote(NO_UNIT)
body = c.get(url).text
print("--- back link when opened from the no-unit roster ---")
m = re.search(r'&larr; back to ([^<]*)</a>', body)
href = re.search(r'<a href="([^"]*)">&larr; back', body)
print("  href:", href.group(1)[:44], "| label:", m.group(1).strip())
assert "/unit?name=" in href.group(1), "must go back where it came from"

body_ward = c.get("/person?uid=" + UID).text
href = re.search(r'<a href="([^"]*)">&larr; back', body_ward)
print("  from the ward (no back):", href.group(1))
assert href.group(1) == "/ward"

print("\n--- door activity is local and 12 hour ---")
for raw, want_day, want_time in [("2026-08-26T22:04:11Z", "2026-08-26", "4:04 PM"),
                                 ("2026-09-21T21:37:20Z", "2026-09-21", "3:37 PM")]:
    assert want_day in body and want_time in body, (want_day, want_time)
    print(f"  {raw}  ->  {want_day} · {want_time}")
assert "22:04" not in body and "21:37" not in body, "UTC 24-hour time still on the page"
print("  no UTC 24-hour times left on the page")

print("\n--- 'added on' also converted (this one crosses midnight) ---")
assert "2026-05-13" in body, "InvitedOn 2026-05-14T00:50Z is 13 May locally"
print("  InvitedOn 2026-05-14T00:50:26Z -> shown as 2026-05-13")

print("\nALL OK")
