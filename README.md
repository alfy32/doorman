# Doorman

A small self-hosted tool for managing door access at a site that uses the
[Kindoo](https://kindoo.tech) access-control API.

**Unofficial.** Not affiliated with, nor endorsed by, Kindoo.

## What it does

Each manager looks after the people in their own unit. Doorman gives them the
three things that job actually needs:

1. **See my people** — name, email, calling, and *when they last opened a door*
2. **Add someone** — email plus a calling; creates the user and grants the
   doors that unit uses
3. **Remove someone** — when the shared seat licence gets tight

Seats are licensed and capped across every unit sharing the site, so the app
also shows who is drawing on that pool and how much of each unit's allocation
is sitting idle.

### Also included
- **Resend invitation** for anyone who has not accepted theirs
- **Per-person page** — door activity, which doors they can open, an editor for
  changing them, and who added them
- **Account history** — the audit trail for one person, paged 60 days at a time
- **Changes** — a feed of everything that has happened to accounts lately
- **All units** — people per unit, with idle counts, to spot overuse

## Run it

```bash
mkdir -p local
cp config.example.json local/config.json    # then fill it in
./run.sh                                    # -> http://127.0.0.1:8000
```

Needs Python 3.11+. Dependencies: `fastapi`, `uvicorn`, `jinja2`,
`python-multipart`, `itsdangerous` — no build step, no JavaScript framework.

All site configuration lives in that one file, and `local/` is git-ignored so
nothing identifying is ever committed. Override its location with
`DOORMAN_CONFIG=/path/to/config.json`.

## Signing in

Doorman has its own accounts, separate from Kindoo:

- Only addresses listed in `allowed_emails` may register
- Registering **proves control of the address**: you enter your email, a
  single-use link is sent to it, and only that link lets you set a password.
  The same form handles forgotten passwords
- Links expire (24h to sign up, 2h to reset) and die on first use. Only a
  SHA-256 hash of each token is stored, so a copy of the database cannot be
  used to claim an address
- The reply is identical whether or not an address is allowed, so the
  allow-list cannot be probed
- Passwords are stored as PBKDF2-SHA256 (240k iterations, per-user salt)
- Accounts and settings live in SQLite at `local/doorman.db`

### Sending the email
No mail server needed — Doorman sends through any SMTP relay you already have,
such as a Gmail address with an App Password:

```json
"smtp": { "host": "smtp.gmail.com", "port": 587,
          "username": "you@gmail.com", "password": "<app password>",
          "from": "you@gmail.com" }
```

Also set `server.public_url` to an address the recipient can actually open;
links are built from it, so `localhost` will not do.

> Leave `smtp.host` empty and links are written to the server log instead of
> sent. That keeps the app usable before mail is configured, but it is **not**
> verification — configure SMTP before anyone else signs up.

Each manager then supplies their **own Kindoo session token** on the Settings
page. Get one from a signed-in Kindoo tab: DevTools → Network → any
`WebService.asmx` request → Request Headers → `SessionTokenID`.

> ⚠️ A token is **password-equivalent** and **not tied to a device** — anyone
> holding it can open doors. It stays server-side and never reaches the
> browser. A token acts *as* that person: Kindoo attributes every action to
> them, which is why they are per-manager rather than per-installation.
> Signing out of Kindoo invalidates that person's token.

## Deploying

It binds to `127.0.0.1` by default. It serves plain HTTP and has no transport
security of its own, so **do not expose it directly**. Put an authenticating
proxy in front (Cloudflare Tunnel + Access, or similar) for remote access.

### As a systemd service

On the server, from a clone:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
sudo ./deploy/install-service.sh
```

That installs `doorman.service`, enables it at boot, starts it, and waits to
confirm something is actually listening before claiming success. It runs as the
user who invoked `sudo`, never as root.

A system install keeps its data outside the checkout, so re-cloning or
`git clean` cannot take the database with it:

| | |
|---|---|
| `/etc/doorman/config.json` | site config; mode 0600, read-only to the service |
| `/var/lib/doorman/` | database and session key (`StateDirectory`) |
| `journalctl -u doorman` | logs |

The installer seeds both from `local/` if it finds them, copying rather than
moving, and never overwrites a file that is already there. Config resolution is
`$DOORMAN_CONFIG`, then `local/config.json`, then `/etc/doorman/config.json` --
so a checkout you are working in still uses its own config on a machine that
also runs the service. `$DOORMAN_DATA_DIR` does the same for the database.

Day to day:

```bash
sudo systemctl status doorman      # is it up
sudo systemctl restart doorman     # after a git pull
journalctl -u doorman -f           # follow the log
```

Because it stays on `127.0.0.1`, reach it from another machine over SSH rather
than by changing the bind address:

```bash
ssh -L 8000:127.0.0.1:8000 you@server    # then open http://127.0.0.1:8000
```

## Layout

```
doorman/
  kindoo.py     API client -- owns the protocol quirks (see below)
  units.py      whitespace-tolerant unit-name matching
  store.py      SQLite: accounts and per-manager settings
  auth.py       sign-up allow-list, password hashing
  config.py     loads local/config.json; no secrets or site data in source
  web/app.py    routes
  web/static/   stylesheet, served with a content hash for cache-busting
local/          site config, database, session key (git-ignored)
deploy/         systemd unit and installer for running it as a service
```

## Notes on the Kindoo API

It is not a conventional JSON API, and the client compensates:

| Behaviour | Consequence |
|---|---|
| Parameters must be **form-urlencoded** | A JSON body is accepted and silently ignored, then the server errors on the "missing" params |
| Responses have `{"d":null}` appended | Strict JSON parsers fail; decode tolerantly |
| Responses are gzipped regardless of `Accept-Encoding` | Sniff and decompress |
| Errors arrive as **HTTP 303** + a bare text code | Never let the client follow redirects, or failures get masked |
| Writes return a bare JSON scalar on success | `null`, `true` or `"1"` — all mean it worked; only a non-JSON body is a failure |
| Date ranges are read as **UTC** | Passing local time silently truncates the window and hides recent data |
| Date ranges cap at 90 days | Page longer queries in slices |
| "Optional" params are often required | Send them as empty strings |
| Some endpoints are role-gated | Granting door access is permissive; revoking it is not |

The published OpenAPI spec is also **incomplete** — the app relies on at least
one endpoint that is absent from it but live on the same host.

Values that look like "all" may act as filters: passing an id of `0` can return
an empty set rather than everything.
