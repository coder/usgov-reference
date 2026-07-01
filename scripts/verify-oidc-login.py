#!/usr/bin/env python3
"""
verify-oidc-login.py - drive a real Coder OIDC login through Keycloak for a
persona user and report the org membership, org roles, and groups that IdP sync
assigned. Proves the Keycloak -> Coder sync end to end.

Usage:
    DEMO_USER_PASSWORD=... python3 scripts/verify-oidc-login.py dana.dev [more...]

Read-only against Coder (it only logs in and GETs). Uses a fresh cookie jar per
user so there is no cached SSO session.
"""
import sys
import re
import json
import os
import http.cookiejar
import urllib.request
import urllib.parse
import urllib.error

CODER = os.environ.get("DEMO_CODER_URL", "https://dev.<BASE_DOMAIN>").rstrip("/")
PW = os.environ["DEMO_USER_PASSWORD"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def opener():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NoRedirect)
    op.addheaders = [("User-Agent", "coder-demo-verify/1.0")]
    return op, cj


def req(op, url, data=None, ctype=None):
    headers = {}
    if ctype:
        headers["Content-Type"] = ctype
    r = urllib.request.Request(url, data=data, headers=headers)
    try:
        resp = op.open(r)
        return resp.getcode(), resp.headers, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode("utf-8", "replace")


def login(user):
    op, cj = opener()
    # 1. Initiate OIDC at Coder -> 302 to Keycloak authorize.
    code, h, _ = req(op, CODER + "/api/v2/users/oidc/callback")
    if code not in (302, 307) or "Location" not in h:
        return None, f"initiate: expected redirect, got {code}"
    authz = h["Location"]
    # 2. Load the Keycloak login page.
    code, h, body = req(op, authz)
    if code != 200:
        # Could be a direct 302 if already authenticated (should not happen on a
        # fresh jar). Surface for debugging.
        return None, f"authorize: expected 200 login page, got {code} loc={h.get('Location')}"
    m = re.search(r'action="([^"]+)"', body)
    if not m:
        return None, "authorize: could not find login form action"
    action = m.group(1).replace("&amp;", "&")
    # 3. Submit credentials -> 302 back to the Coder callback with code+state.
    form = urllib.parse.urlencode({"username": user, "password": PW, "credentialId": ""}).encode()
    code, h, body = req(op, action, data=form,
                        ctype="application/x-www-form-urlencoded")
    if code not in (302, 307) or "Location" not in h:
        return None, f"login POST: expected redirect, got {code} (bad credentials or extra form field?)"
    cb = h["Location"]
    if "/oidc/callback" not in cb:
        return None, f"login POST: unexpected redirect {cb[:120]}"
    # 4. Coder consumes the code, sets the session cookie, redirects to the app.
    code, h, _ = req(op, cb)
    if code not in (302, 307):
        return None, f"coder callback: expected redirect, got {code}"
    tok = None
    for c in cj:
        if c.name == "coder_session_token":
            tok = c.value
    if not tok:
        return None, "coder callback: no coder_session_token cookie set"
    return tok, None


def capi(tok, path):
    r = urllib.request.Request(CODER + path, headers={"Coder-Session-Token": tok})
    try:
        return json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e:
        return {"ERROR": e.code, "body": e.read().decode()[:200]}


def report(user):
    tok, err = login(user)
    if err:
        print(f"\n## {user}: LOGIN FAILED: {err}")
        return
    me = capi(tok, "/api/v2/users/me")
    orgs = capi(tok, "/api/v2/users/me/organizations")
    site_roles = [r["name"] if isinstance(r, dict) else r for r in me.get("roles", [])]
    print(f"\n## {user}  ({me.get('email')})  site_roles={site_roles or '[]'}")
    if isinstance(orgs, dict):
        print("  orgs ERROR:", orgs)
        return
    for o in orgs:
        oid = o["id"]
        # roles for this member in this org
        members = capi(tok, f"/api/v2/organizations/{oid}/members")
        roles = []
        if isinstance(members, list):
            for mem in members:
                if mem.get("user_id") == me["id"]:
                    roles = [r["name"] if isinstance(r, dict) else r for r in mem.get("roles", [])]
        # groups in this org the user belongs to
        groups = capi(tok, f"/api/v2/organizations/{oid}/groups")
        my_groups = []
        if isinstance(groups, list):
            for g in groups:
                ids = [mm.get("id") for mm in (g.get("members") or [])]
                if me["id"] in ids and g["name"] != "Everyone":
                    my_groups.append(g["name"])
        print(f"  org {o['name']:8} display={o.get('display_name'):22} roles={roles or ['member']} groups={my_groups}")


if __name__ == "__main__":
    users = sys.argv[1:] or ["dana.dev"]
    for u in users:
        report(u)
