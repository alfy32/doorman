"""Local database (SQLite -- never committed).

Holds Doorman's own accounts and each manager's runtime settings. Site-wide
reference data (environment id, canonical unit names, and the list of email
addresses allowed to sign up) stays in config.json, which is hand-edited.

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
    seat_allocation  INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
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


def seed_from_config(cfg):
    """First run only: pre-create accounts named in config.json so an existing
    setup keeps working. They still have to choose a password at sign-up."""
    if list_accounts():
        return False
    raw = cfg.get("users") or {}
    if isinstance(raw, list):
        raw = {u.get("key") or u.get("name"): u for u in raw}
    made = False
    for _, u in (raw or {}).items():
        email = (u or {}).get("email")
        if not email:
            continue
        create_account(email, (u or {}).get("name") or "")
        update_account(email, token=(u or {}).get("token") or "",
                       unit=(u or {}).get("unit") or "",
                       door_ids=(u or {}).get("door_ids") or [],
                       seat_allocation=int((u or {}).get("seat_allocation") or 0))
        made = True
    return made
