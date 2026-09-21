"""Configuration: one JSON file, plus environment overrides.

Everything site-specific -- credentials, environment id, unit names -- lives in
ONE file that is never committed:

    local/config.json          (git-ignored; copy config.example.json to start)
    /etc/doorman/config.json   (system install; see deploy/install-service.sh)

Override the location with DOORMAN_CONFIG. Any value can also be supplied by an
environment variable (KINDOO_TOKEN, KINDOO_EID, ...), which wins over the file,
so a deployment can inject secrets without editing anything.
"""
import json, os
from pathlib import Path

from . import store

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config.example.json"


ETC = Path("/etc/doorman/config.json")


def config_path():
    """First of: $DOORMAN_CONFIG, local/config.json, /etc/doorman/config.json.

    local/ wins over /etc so a checkout you are working in keeps using its own
    config even on a machine that also has Doorman installed system-wide.
    """
    env = os.environ.get("DOORMAN_CONFIG")
    if env:
        return Path(env)
    site = ROOT / "local" / "config.json"
    if site.exists():
        return site
    return ETC if ETC.exists() else EXAMPLE


def load(path=None):
    path = Path(path) if path else config_path()
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise SystemExit(f"\n{path} is not valid JSON:\n  {e}\n")


class Settings:
    def __init__(self):
        self.path = config_path()
        self.data = load(self.path)
        self.using_example = self.path == EXAMPLE

    def _get(self, env_name, *keys, default=None, required=False):
        v = os.environ.get(env_name)
        if v in (None, ""):
            v = self.data
            for k in keys:
                v = (v or {}).get(k) if isinstance(v, dict) else None
        if v in (None, "") and required:
            raise SystemExit(
                f"\nMissing {'.'.join(keys)} (or ${env_name}).\n"
                f"Edit {self.path}\n"
                f"  cp config.example.json local/config.json\n")
        return default if v in (None, "") else v

    # ---- accounts ---------------------------------------------------------
    # Signing in to Doorman is email + password (accounts table). Talking to
    # Kindoo uses that same person's own session token, stored on their account,
    # because a token acts AS them -- Kindoo attributes every action to them.
    @property
    def allowed_emails(self):
        """Addresses permitted to sign up. Hand-edited in config.json."""
        return self.data.get("allowed_emails") or []

    def account(self, email):
        return store.get_account(email)

    def token_for_account(self, account):
        tok = os.environ.get("KINDOO_TOKEN") or (account or {}).get("token")
        return str(tok) if tok else None

    # ---- site connection --------------------------------------------------
    @property
    def eid(self):   return int(self._get("KINDOO_EID", "kindoo", "eid", required=True))
    @property
    def base(self):  return str(self._get("KINDOO_BASE", "kindoo", "base_url",
                        default="https://api.kindoo.tech/v3/webservice.asmx"))
    @property
    def host(self):  return str(self._get("DOORMAN_HOST", "server", "host", default="127.0.0.1"))
    @property
    def port(self):  return int(self._get("DOORMAN_PORT", "server", "port", default=8000))

    # ---- units -----------------------------------------------------------
    # One list, each entry describing a unit completely:
    #
    #   {"name": "Foo 1st Unit",           canonical name (required)
    #    "town": "Foo",                    optional; used for bare-town matching
    #    "write_as": "Foo  1st Unit",      optional; how to WRITE it, if different
    #    "aliases": ["Fooo 1st Unit"]}     optional; other spellings seen in data
    #
    # A plain string is accepted as shorthand for {"name": <string>}.
    # Whether a town is ambiguous is DERIVED: a town naming exactly one unit can
    # be matched on its own; a town with several never is.

    @property
    def unit_entries(self):
        out = []
        for raw in self.data.get("units", []) or []:
            entry = {"name": raw} if isinstance(raw, str) else dict(raw or {})
            name = (entry.get("name") or "").strip()
            if name:
                entry["name"] = name
                out.append(entry)
        return out

    @property
    def units(self):
        return [u["name"] for u in self.unit_entries]

    @property
    def aliases(self):
        out = {}
        for u in self.unit_entries:
            for a in u.get("aliases") or []:
                if str(a).strip():
                    out[str(a).strip()] = u["name"]
        return out

    def _towns(self):
        towns = {}
        for u in self.unit_entries:
            town = (u.get("town") or "").strip().casefold()
            if town:
                towns.setdefault(town, []).append(u["name"])
        return towns

    @property
    def one_unit_towns(self):
        """Towns naming exactly one unit -- safe to match on their own."""
        return {t: names[0] for t, names in self._towns().items() if len(names) == 1}

    @property
    def numbered_towns(self):
        """Towns naming several units -- never guessed, because this decides
        building access."""
        return [t for t, names in self._towns().items() if len(names) > 1]

    @property
    def unit_write_names(self):
        """canonical -> exact text to WRITE. Reading is whitespace-tolerant,
        but new records should match the spelling already in use."""
        return {u["name"]: u["write_as"] for u in self.unit_entries if u.get("write_as")}

    def write_name(self, unit):
        return self.unit_write_names.get(unit) or unit

    # ---- mail and service accounts ---------------------------------------
    @property
    def smtp(self):
        """Outgoing mail. Any provider's relay -- no mail server of our own."""
        return self.data.get("smtp") or {}

    @property
    def public_url(self):
        """Base for links we email. Must be reachable by the recipient, so it
        cannot be derived from a request that arrived on localhost."""
        url = self._get("DOORMAN_PUBLIC_URL", "server", "public_url", default="")
        return str(url).rstrip("/")

    @property
    def automated_accounts(self):
        """Service accounts that create users in bulk, shown as 'an automated
        sync' rather than an unfamiliar address."""
        return [a.strip().casefold()
                for a in (self.data.get("automated_accounts") or []) if a.strip()]

    def is_automated(self, email):
        e = (email or "").strip().casefold()
        return bool(e) and any(e == a or e.startswith(a) for a in self.automated_accounts)

    @property
    def seat_allocation(self):
        """Seats each unit is expected to stay within.

        One number for the whole site: it is policy, the same for every unit,
        and not something a manager sets for themselves. 0 or absent means
        usage is shown but not measured against anything.
        """
        try:
            return int(self.data.get("seat_allocation") or 0)
        except (TypeError, ValueError):
            return 0

    def allocation_for(self, unit):
        """The allocation for a real configured unit; None for pseudo-rows
        such as 'no unit', which are not units and cannot be over anything."""
        if not unit or unit not in self.units:
            return None
        return self.seat_allocation or None


    # ---- persistence (database) ------------------------------------------
    def update_account(self, email, **fields):
        return store.update_account(email, **fields)


settings = Settings()
