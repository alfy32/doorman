"""Local database (SQLite -- never committed).

Holds Doorman's own accounts and each manager's runtime settings, including
each manager's Kindoo token -- which is why nothing here belongs in a config
file. Site-wide reference data (environment id, canonical unit names, and the
list of email addresses allowed to sign up) stays in config.json, hand-edited.

Only this module knows about SQLite; if a server database is ever needed, the
rest of the app is unaffected.
"""
import json, os, sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Mutable state -- the database and the session key -- lives here. Defaults to
# local/ in the checkout, which suits running from a clone. A system install
# sets DOORMAN_DATA_DIR=/var/lib/doorman so the data outlives the checkout.
DATA_DIR = Path(os.environ.get("DOORMAN_DATA_DIR") or ROOT / "local")
DB_PATH = DATA_DIR / "doorman.db"

SCHEMA = """
-- Superseded by `accounts` when sign-in moved to email + password.
DROP TABLE IF EXISTS managers;
-- Superseded by temp_people + temp_visits below, which separate the person
-- from the window. It existed for a day and held one failed row.
DROP TABLE IF EXISTS temp_users;

CREATE TABLE IF NOT EXISTS verifications (
    token_hash  TEXT PRIMARY KEY,          -- sha256 of the emailed token
    email       TEXT NOT NULL,
    purpose     TEXT NOT NULL,             -- 'signup' | 'reset'
    expires_at  TEXT NOT NULL,             -- UTC isoformat
    used_at     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS accounts (
    email            TEXT PRIMARY KEY,          -- also the login name
    name             TEXT NOT NULL DEFAULT '',
    password_hash    TEXT NOT NULL DEFAULT '',  -- empty = signed up but no password yet
    token            TEXT NOT NULL DEFAULT '',  -- that person's own Kindoo session token
    unit             TEXT NOT NULL DEFAULT '',
    door_ids         TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Temporary people, and their visits. Two tables, because they are two things.
--
-- A temporary PERSON is a saved contact: who they are, what Kindoo should call
-- them, and which doors they get. That is what a manager keeps and edits -- the
-- piano tuner is the same piano tuner every time.
--
-- A VISIT is one window of access for one of those people. It is never part of
-- the person: the tuner comes for two hours in March and a whole day in June,
-- and neither of those is a fact about the tuner. Visits are also where the
-- Kindoo user lives, and most visits have none -- a seat is spent only while a
-- window is open, so a visit is planned before, and remembered after, any
-- Kindoo user exists for it.
--
-- `owner` scopes both. Each manager keeps their own people and their own
-- visits, and never sees another manager's.
CREATE TABLE IF NOT EXISTS temp_people (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    owner        TEXT NOT NULL,             -- account email; scopes the list
    unit         TEXT NOT NULL DEFAULT '',
    email        TEXT NOT NULL DEFAULT '',
    name         TEXT NOT NULL DEFAULT '',  -- for our list; Kindoo has none until they accept
    description  TEXT NOT NULL DEFAULT '',  -- the text Kindoo shows: calling, or why
    door_ids     TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
-- One row per person per manager, so "pick someone from the list" has one
-- obvious answer and scheduling them twice cannot quietly fork the record.
CREATE UNIQUE INDEX IF NOT EXISTS temp_people_one_each
    ON temp_people (owner, email);

CREATE TABLE IF NOT EXISTS temp_visits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id    INTEGER NOT NULL,
    owner        TEXT NOT NULL,
    starts_at    TEXT NOT NULL DEFAULT '',  -- UTC isoformat
    ends_at      TEXT NOT NULL DEFAULT '',  -- UTC isoformat
    kindoo_uid   TEXT NOT NULL DEFAULT '',  -- empty until they exist in Kindoo
    kindoo_euid  TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'planned',   -- planned|live|ended|failed
    note         TEXT NOT NULL DEFAULT '',  -- what went wrong, if anything
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS temp_visits_owner ON temp_visits (owner, ends_at);
CREATE INDEX IF NOT EXISTS temp_visits_due ON temp_visits (status, starts_at);
"""


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _row(r):
    d = dict(r)
    try:
        d["door_ids"] = json.loads(d.get("door_ids") or "[]")
    except json.JSONDecodeError:
        d["door_ids"] = []
    d["key"] = d["email"]                    # callers treat email as the id
    return d


def get_account(email):
    if not email:
        return None
    with connect() as con:
        r = con.execute("SELECT * FROM accounts WHERE email = ?",
                        (email.strip().casefold(),)).fetchone()
        return _row(r) if r else None


def list_accounts():
    with connect() as con:
        return [_row(r) for r in con.execute("SELECT * FROM accounts ORDER BY email")]


def create_account(email, name="", password_hash=""):
    with connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO accounts (email, name, password_hash) VALUES (?,?,?)",
            (email.strip().casefold(), name, password_hash))
    return get_account(email)


def update_account(email, **fields):
    if not fields:
        return get_account(email)
    if "door_ids" in fields:
        fields["door_ids"] = json.dumps(list(fields["door_ids"]))
    with connect() as con:
        cols = ", ".join(f"{k} = ?" for k in fields)
        con.execute(f"UPDATE accounts SET {cols} WHERE email = ?",
                    (*fields.values(), email.strip().casefold()))
    return get_account(email)


def delete_account(email):
    with connect() as con:
        con.execute("DELETE FROM accounts WHERE email = ?", (email.strip().casefold(),))


# ---- email verification ---------------------------------------------------
# Only the hash of a token is stored, so the database never holds anything that
# could be used to claim an address -- the secret exists only in the email.

def add_verification(token_hash, email, purpose, expires_at):
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO verifications "
                    "(token_hash, email, purpose, expires_at) VALUES (?,?,?,?)",
                    (token_hash, email.strip().casefold(), purpose, expires_at))


def get_verification(token_hash):
    with connect() as con:
        r = con.execute("SELECT * FROM verifications WHERE token_hash = ?",
                        (token_hash,)).fetchone()
        return dict(r) if r else None


def use_verification(token_hash, when):
    with connect() as con:
        con.execute("UPDATE verifications SET used_at = ? WHERE token_hash = ?",
                    (when, token_hash))


def purge_verifications(before):
    """Drop spent and expired tokens; nothing here is worth keeping."""
    with connect() as con:
        con.execute("DELETE FROM verifications WHERE expires_at < ? OR used_at IS NOT NULL",
                    (before,))




# ---- temporary people, and their visits -----------------------------------
# Every read is scoped by owner. Passing the owner in rather than filtering
# afterwards means a missing scope is a missing argument, not a silent leak of
# one manager's list into another's.

def _json_row(r, *fields):
    d = dict(r)
    for f in fields:
        try:
            d[f] = json.loads(d.get(f) or "[]")
        except json.JSONDecodeError:
            d[f] = []
    return d


def _owner(email):
    return (email or "").strip().casefold()


# -- the people ------------------------------------------------------------

def save_temp_person(owner, email, **fields):
    """Create or update one saved person, keyed on their address.

    Upsert rather than insert: a manager typing an address they have used
    before means "this person", not "a second copy of this person".
    """
    owner, email = _owner(owner), (email or "").strip()
    if "door_ids" in fields:
        fields["door_ids"] = json.dumps(list(fields["door_ids"]))
    with connect() as con:
        cur = con.execute(
            "INSERT INTO temp_people (owner, email) VALUES (?,?) "
            "ON CONFLICT (owner, email) DO NOTHING", (owner, email))
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            con.execute(f"UPDATE temp_people SET {cols} WHERE owner = ? AND email = ?",
                        (*fields.values(), owner, email))
        r = con.execute("SELECT * FROM temp_people WHERE owner = ? AND email = ?",
                        (owner, email)).fetchone()
    return _json_row(r, "door_ids")


def list_temp_people(owner):
    with connect() as con:
        return [_json_row(r, "door_ids") for r in con.execute(
            "SELECT * FROM temp_people WHERE owner = ? ORDER BY id DESC", (_owner(owner),))]


def get_temp_person(owner, person_id):
    with connect() as con:
        r = con.execute("SELECT * FROM temp_people WHERE id = ? AND owner = ?",
                        (person_id, _owner(owner))).fetchone()
        return _json_row(r, "door_ids") if r else None


def update_temp_person(owner, person_id, **fields):
    if not fields:
        return get_temp_person(owner, person_id)
    if "door_ids" in fields:
        fields["door_ids"] = json.dumps(list(fields["door_ids"]))
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE temp_people SET {cols} WHERE id = ? AND owner = ?",
                    (*fields.values(), person_id, _owner(owner)))
    return get_temp_person(owner, person_id)


def delete_temp_person(owner, person_id):
    """Forget a person and everything ever scheduled for them."""
    with connect() as con:
        con.execute("DELETE FROM temp_visits WHERE person_id = ? AND owner = ?",
                    (person_id, _owner(owner)))
        con.execute("DELETE FROM temp_people WHERE id = ? AND owner = ?",
                    (person_id, _owner(owner)))


# -- their visits ----------------------------------------------------------

def add_temp_visit(owner, person_id, starts_at, ends_at):
    with connect() as con:
        cur = con.execute(
            "INSERT INTO temp_visits (owner, person_id, starts_at, ends_at) "
            "VALUES (?,?,?,?)", (_owner(owner), person_id, starts_at, ends_at))
        return cur.lastrowid


def list_temp_visits(owner, person_id=None):
    """Newest window first. Every row carries its person, because a visit on
    its own says nothing a manager can read."""
    sql = ("SELECT v.*, p.email, p.name, p.description, p.door_ids, p.unit "
           "FROM temp_visits v JOIN temp_people p ON p.id = v.person_id "
           "WHERE v.owner = ?")
    args = [_owner(owner)]
    if person_id is not None:
        sql += " AND v.person_id = ?"
        args.append(person_id)
    with connect() as con:
        return [_json_row(r, "door_ids")
                for r in con.execute(sql + " ORDER BY v.ends_at DESC, v.id DESC", args)]


def get_temp_visit(owner, visit_id):
    with connect() as con:
        r = con.execute(
            "SELECT v.*, p.email, p.name, p.description, p.door_ids, p.unit "
            "FROM temp_visits v JOIN temp_people p ON p.id = v.person_id "
            "WHERE v.id = ? AND v.owner = ?", (visit_id, _owner(owner))).fetchone()
        return _json_row(r, "door_ids") if r else None


def update_temp_visit(owner, visit_id, **fields):
    if not fields:
        return get_temp_visit(owner, visit_id)
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE temp_visits SET {cols} WHERE id = ? AND owner = ?",
                    (*fields.values(), visit_id, _owner(owner)))
    return get_temp_visit(owner, visit_id)


def delete_temp_visit(owner, visit_id):
    with connect() as con:
        con.execute("DELETE FROM temp_visits WHERE id = ? AND owner = ?",
                    (visit_id, _owner(owner)))


def due_temp_visits(before, now):
    """Planned visits whose window is about to open, across ALL owners.

    The only unscoped read of these tables, and deliberately so: the scheduler
    works on behalf of every manager at once, and each row carries the owner
    whose token will be used. Windows that have already closed are left alone --
    creating a user Kindoo would expire on sight spends a seat on nothing.
    """
    with connect() as con:
        return [_json_row(r, "door_ids") for r in con.execute(
            "SELECT v.*, p.email, p.name, p.description, p.door_ids, p.unit "
            "FROM temp_visits v JOIN temp_people p ON p.id = v.person_id "
            "WHERE v.status = 'planned' AND v.starts_at <= ? AND v.ends_at > ? "
            "ORDER BY v.starts_at", (before, now))]
