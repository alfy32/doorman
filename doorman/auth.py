"""Sign-up, sign-in, and the allow-list that gates them.

Doorman has two unrelated identities and they must not be confused:

  * the Doorman account  -- email + password, used to sign in to THIS app
  * the Kindoo token     -- that person's own session token, used to talk to
                            Kindoo as them

Only email addresses listed in config.json ("allowed_emails") may sign up.
The list is hand-edited by whoever runs the server, so a stranger who finds
the URL cannot create an account.
"""
import datetime as dt
import hashlib, hmac, os, secrets

from . import store

ITERATIONS = 240_000


def hash_password(password):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password, stored):
    try:
        algo, iters, salt_hex, want = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(iters))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(dk.hex(), want)


def normalise(email):
    return (email or "").strip().casefold()


def is_allowed(email, allowed):
    """Allow-listed? Comparison is case-insensitive and whitespace-tolerant."""
    return normalise(email) in {normalise(a) for a in (allowed or [])}


# ---- proving someone owns an address --------------------------------------
# A random token goes to the address; only its hash is kept here. Holding the
# token is the proof, so it is single-use and short-lived.

SIGNUP_HOURS = 24
RESET_HOURS = 2


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)


def issue_token(email, purpose):
    """Create a verification token. Returns the secret to email; only its hash
    is stored, so a copy of the database cannot be used to claim an address."""
    hours = SIGNUP_HOURS if purpose == "signup" else RESET_HOURS
    token = secrets.token_urlsafe(32)
    store.purge_verifications(_utcnow().isoformat())
    store.add_verification(_hash_token(token), normalise(email), purpose,
                           (_utcnow() + dt.timedelta(hours=hours)).isoformat())
    return token


def check_token(token, purpose):
    """(email, None) if the token is good, else (None, reason)."""
    row = store.get_verification(_hash_token(token or ""))
    if not row or row["purpose"] != purpose:
        return None, "That link isn't valid."
    if row["used_at"]:
        return None, "That link has already been used."
    if row["expires_at"] < _utcnow().isoformat():
        return None, "That link has expired."
    return row["email"], None


def consume_token(token):
    store.use_verification(_hash_token(token), _utcnow().isoformat())


def password_problem(password, confirm=None):
    """None if acceptable, else a message to show the person."""
    if confirm is not None and password != confirm:
        return "Those passwords don't match."
    if len(password or "") < 10:
        return "Use at least 10 characters."
    return None
