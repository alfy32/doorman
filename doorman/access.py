"""Trusting Cloudflare Access, so there is only one login.

Behind a Cloudflare Access application, every request that reaches this app has
already been authenticated at Cloudflare's edge, which signs a JWT naming the
verified address. Validating that JWT lets Doorman sign the person in without
asking for a password a second time.

The signature is checked against Cloudflare's published keys, and the audience
and issuer must match this specific application -- a JWT minted for someone
else's Access app is refused. A plain header is never trusted.

This is only safe while the app cannot be reached except through Cloudflare.
Anything able to talk to the port directly could present its own cookie, and
although it could not forge a signature, it would not have to if this were
ever pointed at the wrong team. Hence: off unless configured, and bind to
127.0.0.1 before turning it on.
"""
import logging
from urllib.parse import quote

import jwt
from jwt import PyJWKClient

log = logging.getLogger("doorman.access")

_clients = {}          # team domain -> PyJWKClient, which caches the keys


def _keys(team_domain):
    client = _clients.get(team_domain)
    if client is None:
        client = PyJWKClient(f"https://{team_domain}/cdn-cgi/access/certs",
                             cache_keys=True, lifespan=3600)
        _clients[team_domain] = client
    return client


def token_from(request):
    """Cloudflare sends the assertion as a header, and as a cookie on the
    browser's first hop. Either is the same signed token."""
    return (request.headers.get("cf-access-jwt-assertion")
            or request.cookies.get("CF_Authorization"))


def verified_email(request, cfg):
    """The address Cloudflare vouched for, or None.

    None means "no opinion" -- the caller falls back to Doorman's own login
    rather than treating it as a failure.
    """
    if not (cfg.get("enabled") and cfg.get("team_domain") and cfg.get("aud")):
        return None
    token = token_from(request)
    if not token:
        return None
    try:
        key = _keys(cfg["team_domain"]).get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"],
                            audience=cfg["aud"],
                            issuer=f"https://{cfg['team_domain']}")
    except Exception as e:                    # bad signature, wrong app, expired
        log.warning("Cloudflare Access token rejected: %s", e)
        return None
    email = (claims.get("email") or "").strip().casefold()
    return email or None


def logout_url(cfg, return_to=None):
    """Where to send someone so Cloudflare forgets them too. Clearing only the
    local session would be pointless: the next request re-authenticates.

    `return_to` bounces them straight back to the site afterwards, where Access
    asks them to sign in again -- otherwise they land on a bare Cloudflare page
    with no way back.
    """
    if not (cfg.get("enabled") and cfg.get("team_domain")):
        return None
    url = f"https://{cfg['team_domain']}/cdn-cgi/access/logout"
    if return_to:
        url += "?returnTo=" + quote(return_to, safe="")
    return url
