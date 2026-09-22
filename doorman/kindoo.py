"""Kindoo API client.

Every quirk below was found the hard way against the live API on 2026-09-20;
see KINDOO-API-FINDINGS.md. In short, this is NOT a normal JSON API:

  * Parameters must be form-urlencoded. A JSON body is accepted and SILENTLY
    IGNORED -- the server then null-derefs on the missing params.
  * Responses have `{"d":null}` appended after the real payload.
  * Responses are gzipped whether or not you ask.
  * Errors are HTTP 303 with a bare text code ("NoPermission"). Never follow it.
  * Writes signal success with a bare JSON scalar, and which one varies:
    `null`, `true`, or `"1"` depending on the endpoint. Any of them means it
    worked -- only a non-JSON body (e.g. `NoPermission`) is a failure.
  * Params the OpenAPI spec calls optional are often actually required -- pass
    them as empty strings.
"""
import gzip, json, logging, urllib.error, urllib.parse, urllib.request

log = logging.getLogger("doorman.kindoo")


class KindooError(RuntimeError):
    def __init__(self, endpoint, status, body):
        self.endpoint, self.status, self.body = endpoint, status, body
        super().__init__(f"{endpoint} -> HTTP {status}: {body[:200]}")

    @property
    def is_auth(self):      return self.status == 401
    @property
    def is_permission(self): return "NoPermission" in self.body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Kindoo signals errors with 303; following it would hide the failure."""
    def redirect_request(self, *a, **kw): return None


class Kindoo:
    def __init__(self, token, eid, base):
        self.token, self.eid, self.base = token, eid, base
        self._opener = urllib.request.build_opener(_NoRedirect)

    def call(self, endpoint, **params):
        body = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(
            f"{self.base}/{endpoint}", data=body,
            headers={"SessionTokenID": self.token,
                     "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self._opener.open(req, timeout=30) as r:
                data, status = r.read(), r.status
        except urllib.error.HTTPError as e:
            data, status = e.read(), e.code
        except urllib.error.URLError as e:
            raise KindooError(endpoint, 0, f"network error: {e.reason}") from e
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        raw = data.decode("utf-8-sig", "replace").strip()
        if status != 200:
            raise KindooError(endpoint, status, raw)
        if raw == "":
            return None
        try:
            # raw_decode also discards the trailing {"d":null} the API appends.
            return json.JSONDecoder().raw_decode(raw)[0]
        except json.JSONDecodeError:
            # Not JSON at all -- an error code in the body despite the 200.
            raise KindooError(endpoint, status, raw) from None

    # ---- reads -----------------------------------------------------------
    def environment(self):
        envs = self.call("KindooGetEnvironments") or []
        return envs[0] if envs else {}

    def users(self, start=0, end=400, keyword=""):
        return self.call("KindooGetEnvironmentUsersLight", EID=self.eid,
                         Start=start, End=end, KeyWord=keyword,
                         FetchInvitedOnInvitedByData="true") or []

    def entry_points(self):
        return self.call("KindooGetEnvironmentEntryPoints", EID=self.eid) or []

    def user_by_email(self, email):
        for u in self.users():
            if (u.get("Username") or "").casefold() == email.casefold():
                return u
        return None

    # ---- writes ----------------------------------------------------------
    def invite_user(self, email, description, role=2, temp=False,
                    expiry=None, starts=None, timezone=None):
        """Create a site user. role 2 = guest (a normal door-opener).

        `UsersEmail` is an array of objects, so it goes over the wire as a
        JSON string inside the form encoding.
        """
        person = {"UserEmail": email, "UserRole": role,
                  "Description": description or "", "CCInEmail": False,
                  "IsTempUser": bool(temp)}
        if temp:
            person.update({"ExpiryDate": expiry, "StartAccessDoorsDate": starts,
                           "ExpiryTimeZone": timezone})
        return self.call("KindooCheckUserTypeAndInviteAccordingToType",
                         EID=self.eid, UsersEmail=json.dumps([person]))

    def grant_always_access(self, uid, entry_point_ids):
        """Give `uid` permanent ('always') access to specific doors."""
        return self.call("KindooSaveAlwaysAccessRight", EID=self.eid, UID=uid,
                         ALL="false", EntryPointIDs=json.dumps(list(entry_point_ids)))

    def edit_description(self, euid, description):
        """Change the free-text description on a user.

        Takes **EUID**, not UID -- the same person carries both, and this is
        the endpoint that wants the environment one.

        Kindoo refuses to let a token edit its own user record: that returns
        303 NoPermission while edits to everyone else succeed.
        """
        return self.call("KindooEditEnvironmentUserDescription", EID=self.eid,
                         EUID=euid, Description=description or "")

    def resend_invitation(self, uid, cc_manager=False):
        """Have Kindoo email the invitation again.

        UNDOCUMENTED: KindooResendInvitationEmail appears in none of the 157
        endpoints of the published OpenAPI spec, but it exists on the same host
        and is what Kindoo's own app calls. Parameter names are case-sensitive
        and unlike the rest of the API -- `userID`, not `UID` or `UserID`.
        Takes the account id (UserID), not the environment id (EUID).
        """
        return self.call("KindooResendInvitationEmail", EID=self.eid, userID=uid,
                         ccInEmail="true" if cc_manager else "false")

    def revoke_access_right(self, right_id):
        """Remove ONE always-access record (the `ID` from access_permissions).

        Finding the call a manager may actually use took some doing; for a
        manager-level token the others are refused outright:
          * KindooRevokeAlwaysAccessRightsByEUIDAndEntryPointIDs -> 401 (both hosts)
          * KindooRevokeAccessRightsByIDSAndEUID                 -> NoPermission
        This one takes EID + a single right id and works. It removes one record
        per call, so revoking several doors means several calls.
        """
        return self.call("KindooRevokeAlwaysAccessRightByID",
                         EID=self.eid, ID=right_id)

    def access_permissions(self, uid):
        return self.call("KindooGetUserAccessPermissions", UID=uid, EID=self.eid,
                         Start=0, End=100, Keyword="",
                         FetchGrantedByData="false") or []

    def revoke_user(self, uid):
        """Remove a user from the site, freeing their seat. Takes UID, not EUID."""
        return self.call("KindooRevokeUserFromEnvironment", EID=self.eid, UID=uid)

    def management_logs(self, start, end):
        """Invite/accept/revoke/edit history. Range must be <= 90 days.

        This is the only audit trail: each row is a revision of one user's
        membership, so consecutive rows can be diffed to see what changed.
        """
        return self.call("KindooGetSiteUserManagementLogs", EID=self.eid,
                         StartDate=start, EndDate=end, KeyWord="") or []

    def access_logs(self, start, end):
        """start/end: 'YYYY-MM-DDTHH:MM:SS'. Range must be <= 90 days."""
        return self.call("KindooGetSiteUserAccessLogs", EID=self.eid,
                         StartDate=start, EndDate=end,
                         KeyWord="", EntryPointID="", UID="", ViewLogType="") or []
