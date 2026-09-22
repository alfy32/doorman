"""Windows, hour rounding, row states, and per-manager scoping.

Run it directly -- there is no test runner to install:

    .venv/bin/python tests/test_temps.py

Kindoo is stubbed throughout. Nothing here touches the live API, and every
database write goes to a throwaway directory, so it is safe to run against a
working checkout.
"""
import datetime as dt, os, sys, tempfile

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
os.environ["DOORMAN_DATA_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("KINDOO_EID", "1")

from doorman import store, temps

print('=== rounding: every window runs to the top of an hour ===')
def at(h, m):      # a local wall-clock time, as UTC
    return dt.datetime(2026, 9, 21, h, m, tzinfo=temps.zone()).astimezone(dt.timezone.utc)
show = lambda w: temps.local_text(temps.to_utc_text(w), "%a %-d %b %-I:%M %p")
for hh, mm in [(9, 50), (9, 0), (9, 1), (23, 30), (16, 59)]:
    now = at(hh, mm)
    print(f"asked at {show(now):24}", end="")
    for key, label, _ in temps.PRESETS:
        a, b = temps.window(key, now=now)
        print(f" | {label}: {show(b)}", end="")
    print()
a, b = temps.window("day", day="2026-10-04", now=at(9, 50))
print("\nwhole day 4 Oct:", show(a), "->", show(b))
a, b = temps.window("range", starts="2026-10-04T17:00", ends="2026-10-04T18:30", now=at(9, 50))
print("typed 17:00-18:30:", show(a), "->", show(b), "(end rounded up)")
a, b = temps.window("range", starts="2026-10-04T17:00", ends="2026-10-04T18:00", now=at(9, 50))
print("typed 17:00-18:00:", show(a), "->", show(b), "(already whole, unchanged)")
# a DST boundary must still produce a real instant
a, b = temps.window("2h", now=dt.datetime(2026, 11, 1, 7, 50, tzinfo=dt.timezone.utc))
print("across the DST change:", show(a), "->", show(b))


print('\n=== windows, states and scoping ===')
tz = temps.zone()
print("zone:", tz)
now = dt.datetime(2026, 9, 21, 14, 30, tzinfo=dt.timezone.utc)   # 8:30am local

for preset, _label, _span in temps.PRESETS:
    a, b = temps.window(preset, now=now)
    print(f"{preset:6} {temps.local_text(temps.to_utc_text(a))} -> {temps.local_text(temps.to_utc_text(b))}")

a, b = temps.window("day", day="2026-10-04", now=now)
print("day   ", temps.local_text(temps.to_utc_text(a)), "->", temps.local_text(temps.to_utc_text(b)))
assert temps.local_text(temps.to_utc_text(a), "%H:%M") == "00:00", "local midnight"
assert temps.local_text(temps.to_utc_text(b), "%H:%M") == "00:00", "runs to the next local midnight"

a, b = temps.window("range", starts="2026-10-04T17:00", ends="2026-10-04T21:00", now=now)
print("range ", temps.local_text(temps.to_utc_text(a)), "->", temps.local_text(temps.to_utc_text(b)))
assert temps.local_text(temps.to_utc_text(a), "%H:%M") == "17:00"

# the Kindoo-facing start is LEAD earlier than the promised one
promised = temps.to_utc_text(a)
print("promised start:", temps.api_text(promised), " sent to Kindoo:", temps.api_text(promised, early=temps.LEAD))
assert temps.api_text(promised, early=temps.LEAD) == "2026-10-04T22:50:00Z"

for bad, why in [(("range",), "no times"), (("nonsense",), "unknown preset")]:
    try:
        temps.window(*bad, now=now); print("NOT REFUSED:", why)
    except temps.WindowError as e:
        print("refused ok:", e)
try:
    temps.window("day", day="2020-01-01", now=now); print("NOT REFUSED: past day")
except temps.WindowError as e:
    print("refused ok:", e)

# ---- states over a row's life ----
def row(status, s, e, uid=""):
    return {"status": status, "starts_at": s, "ends_at": e, "kindoo_uid": uid, "id": 1}
soon, later = temps.to_utc_text(now + dt.timedelta(hours=1)), temps.to_utc_text(now + dt.timedelta(hours=9))
past1, past2 = temps.to_utc_text(now - dt.timedelta(hours=9)), temps.to_utc_text(now - dt.timedelta(hours=1))
for label, r in [("planned, future", row("planned", soon, later)),
                 ("planned, now",    row("planned", past2, later)),
                 ("planned, missed", row("planned", past1, past2)),
                 ("live",            row("live", past2, later, "99")),
                 ("live, past end",  row("live", past1, past2, "99")),
                 ("ended",           row("ended", past1, past2)),
                 ("failed",          row("failed", soon, later))]:
    print(f"  {label:16} -> {temps.state_of(r, now)}")

# ---- storage is per manager ----
a_id = store.add_temp_user("A@x.com", email="one@x.com", starts_at=soon, ends_at=later,
                           door_ids=[1, 2], description="Scout leader", unit="U")
b_id = store.add_temp_user("b@x.com", email="two@x.com", starts_at=soon, ends_at=later)
print("A sees:", [r["email"] for r in store.list_temp_users("a@x.com")])
print("B sees:", [r["email"] for r in store.list_temp_users("b@x.com")])
assert store.get_temp_user("b@x.com", a_id) is None, "must not read another manager's row"
assert store.get_temp_user("a@x.com", a_id)["door_ids"] == [1, 2]
store.update_temp_user("b@x.com", a_id, status="ended")
assert store.get_temp_user("a@x.com", a_id)["status"] == "planned", "must not write another's row"
print("scoping ok")

# ---- the scheduler picks the right rows ----
due = store.due_temp_users(temps.to_utc_text(now + temps.LEAD), temps.to_utc_text(now))
print("due right now:", [r["email"] for r in due], "(none expected: both start in an hour)")
due = store.due_temp_users(temps.to_utc_text(now + dt.timedelta(hours=2)), temps.to_utc_text(now))
print("due within 2h:", sorted(r["email"] for r in due))
store.add_temp_user("a@x.com", email="over@x.com", starts_at=past1, ends_at=past2)
due = store.due_temp_users(temps.to_utc_text(now + dt.timedelta(hours=2)), temps.to_utc_text(now))
assert "over@x.com" not in [r["email"] for r in due], "a window already closed must not be created"
print("closed window skipped ok")
print("\nALL OK")
