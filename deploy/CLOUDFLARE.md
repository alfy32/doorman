# Remote access with a Cloudflare Tunnel

Doorman binds to `127.0.0.1` and has no transport security of its own, so it
must not be exposed directly. A Cloudflare Tunnel gives it an HTTPS hostname
with **no open inbound ports and no dynamic DNS**: `cloudflared` dials out from
the server and Cloudflare routes to it. Cloudflare Access then authenticates
visitors at the edge, before a request reaches the machine at all.

Everything below is free-plan territory (Access covers 50 users).

Placeholders: `doorman.example.com` is the hostname, `example.com` the zone,
`8765` the port Doorman listens on.

## Prerequisites

- A domain whose DNS is on Cloudflare
- Doorman already running as a service (see the README)

## 1. Install cloudflared

```bash
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install -y cloudflared
```

## 2. Authenticate

```bash
cloudflared tunnel login
```

Opens a browser to pick the zone; writes `~/.cloudflared/cert.pem`. On a
headless box it prints a URL to open elsewhere.

## 3. Create the tunnel and its DNS record

```bash
cloudflared tunnel create doorman
cloudflared tunnel route dns doorman doorman.example.com
```

The first prints a **tunnel UUID** and writes `~/.cloudflared/<UUID>.json` --
the tunnel's credentials, worth the same as the tunnel itself. The second adds
a proxied CNAME to `<UUID>.cfargotunnel.com`.

## 4. Configure and run it as a service

**Check for an existing config first.** A box that already runs a tunnel for
something else has one, and overwriting it breaks that service:

```bash
sudo cat /etc/cloudflared/config.yml 2>/dev/null || echo "NONE"
cloudflared tunnel list
```

If one exists, add Doorman as another hostname on the existing tunnel -- one
tunnel serves many hostnames -- rather than creating a second service. If not:

```bash
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/<UUID>.json /etc/cloudflared/doorman.json
sudo chmod 600 /etc/cloudflared/doorman.json

sudo tee /etc/cloudflared/config.yml >/dev/null <<'YAML'
tunnel: <UUID>
credentials-file: /etc/cloudflared/doorman.json
ingress:
  - hostname: doorman.example.com
    service: http://127.0.0.1:8765
  - service: http_status:404
YAML

sudo cloudflared service install
systemctl status cloudflared --no-pager
```

The trailing catch-all with no hostname is mandatory; `cloudflared` refuses to
start without it.

## 5. Put Access in front -- before testing the hostname

Between step 4 and this one, the app is on the public internet behind nothing
but its own login form. Either do this immediately or keep `cloudflared`
stopped until it is done.

At <https://one.dash.cloudflare.com> (first visit asks for a **team name**,
which becomes `<team>.cloudflareaccess.com`, and the Free plan):

**Access controls -> Applications -> Create new application -> Self-hosted**

| Field | Value |
|---|---|
| Application name | `doorman` |
| Session duration | 24 hours |
| Public hostname | subdomain `doorman`, domain `example.com`, empty path |

Then add a policy -- **Action: Allow**, **Include: Emails -> your address**.
Leave **One-time PIN** as the login method; it needs no identity provider and
emails a code.

A policy created under *Reusable components -> Policies* is only a definition:
it does nothing until it is attached to an application.

## 6. Point Doorman at the hostname

In `/etc/doorman/config.json`:

```json
"server": { "host": "127.0.0.1", "port": 8765,
            "public_url": "https://doorman.example.com" }
```

```bash
./deploy/update.sh
```

`public_url` earns its keep twice: emailed links finally resolve for someone
off the network, and the session cookie is marked `Secure` when it starts with
`https://`.

## 7. Optional: one login instead of two

By default a visitor authenticates twice -- Cloudflare, then Doorman. Doorman
can instead trust Cloudflare's signed assertion and sign them in directly.

Take the **Application Audience (AUD) Tag** from the Access application's
overview page, then in `/etc/doorman/config.json`:

```json
"access": {
  "enabled": true,
  "team_domain": "<team>.cloudflareaccess.com",
  "aud": "<AUD tag>"
}
```

```bash
./deploy/update.sh
```

The token is validated properly -- RS256 signature against Cloudflare's
published keys, plus audience and issuer -- so one minted for a different
application, or a header someone simply made up, is refused. An allow-listed
address with no account yet gets one created, because Cloudflare has already
proved they own it.

**Only enable this with `host` set to `127.0.0.1`.** It trusts the edge's
account of who is calling, which holds only while the edge cannot be bypassed.
Signing out redirects to Cloudflare's logout: dropping the local session alone
would re-authenticate on the very next request.

## Verifying

From a machine that is *not* signed in:

```bash
curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" https://doorman.example.com/
```

| Result | Meaning |
|---|---|
| `302` to `<team>.cloudflareaccess.com` | Correct -- Access is enforcing |
| `303` to `/login` | Access is **not** enforcing; Doorman is public |
| `530` | Tunnel is down (error 1033); `cloudflared` not running or not connected |
| `1016` | DNS points at a tunnel that no longer exists |

Access config can take a minute to propagate; a `303` immediately after saving
the policy is worth re-checking before you go hunting. The final test is a
browser on a phone with wifi off -- nothing else proves it is reachable from
outside.

## Living with it

- Access identifies the person; Doorman still asks for its own email and
  password. Two independent layers, deliberately.
- **Adding a manager is two places**: `allowed_emails` in Doorman's config and
  the Access policy. Miss the second and they never reach the login form.
- Whoever controls an allowed inbox can pass Access, since one-time PINs are
  emailed. Those accounts want 2FA.
- Logs: `journalctl -u cloudflared -f`. Access request history lives under
  **Insights & Logs** in the Zero Trust dashboard.
- The tunnel needs no inbound ports. Delete any leftover port-forward on the
  router -- it would be a path to the app that bypasses Access entirely.
