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

# Kindoo is sent LOCAL wall-clock time with no zone marker (it converts using
# ExpiryTimeZone), and the start it is given is LEAD earlier than the promised one
promised = temps.to_utc_text(a)
print("promised start:", temps.api_text(promised), " sent to Kindoo:", temps.api_text(promised, early=temps.LEAD))
assert temps.api_text(promised) == "2026-10-04T17:00:00", temps.api_text(promised)
assert temps.api_text(promised, early=temps.LEAD) == "2026-10-04T16:50:00"

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

# ---- storage: people and visits are separate, and both are per manager ----
alice = store.save_temp_person("A@x.com", "one@x.com", name="One", unit="U",
                               description="Scout leader", door_ids=[1, 2])
bob = store.save_temp_person("b@x.com", "two@x.com", name="Two")
print("A sees:", [p["email"] for p in store.list_temp_people("a@x.com")])
print("B sees:", [p["email"] for p in store.list_temp_people("b@x.com")])
assert store.get_temp_person("b@x.com", alice["id"]) is None, "must not read another manager's person"
assert store.get_temp_person("a@x.com", alice["id"])["door_ids"] == [1, 2]
store.update_temp_person("b@x.com", alice["id"], description="hijacked")
assert store.get_temp_person("a@x.com", alice["id"])["description"] == "Scout leader", \
    "must not write another manager's person"
print("scoping ok")

# the same address twice is the same person, not a second copy
again = store.save_temp_person("a@x.com", "one@x.com", description="Piano tuner")
assert again["id"] == alice["id"], "an address already saved must not fork"
assert again["door_ids"] == [1, 2], "an update must not wipe what it did not touch"
assert len(store.list_temp_people("a@x.com")) == 1
print("re-saving the same address edits the one record:", again["description"])

# a visit holds the window; the person holds nothing about time
v1 = store.add_temp_visit("a@x.com", alice["id"], soon, later)
visit = store.get_temp_visit("a@x.com", v1)
assert visit["email"] == "one@x.com" and visit["description"] == "Piano tuner", \
    "a visit must carry its person"
assert "starts_at" not in store.get_temp_person("a@x.com", alice["id"])
print("visit", v1, "->", visit["email"], visit["starts_at"], "to", visit["ends_at"])
store.add_temp_visit("b@x.com", bob["id"], soon, later)
assert [v["id"] for v in store.list_temp_visits("b@x.com")] != [v1]
print("visits are scoped too")

# ---- the scheduler picks the right rows ----
due = store.due_temp_visits(temps.to_utc_text(now + temps.LEAD), temps.to_utc_text(now))
print("due right now:", [r["email"] for r in due], "(none expected: both start in an hour)")
due = store.due_temp_visits(temps.to_utc_text(now + dt.timedelta(hours=2)), temps.to_utc_text(now))
print("due within 2h:", sorted(r["email"] for r in due))
store.add_temp_visit("a@x.com", alice["id"], past1, past2)
due = store.due_temp_visits(temps.to_utc_text(now + dt.timedelta(hours=2)), temps.to_utc_text(now))
assert past1 not in [r["starts_at"] for r in due], "a window already closed must not be created"
print("closed window skipped ok")

# forgetting a person takes their visits with them
store.delete_temp_person("a@x.com", alice["id"])
assert not store.list_temp_people("a@x.com") and not store.list_temp_visits("a@x.com")
print("forgetting a person clears their visits too")
print("\nALL OK")
