#!/usr/bin/env python3
"""
grant-coder-owner.py - grant the Coder site-wide Owner role to a user so one
Keycloak SSO identity (default austen.platform, the operator super admin) is
super admin across Coder, GitLab, and Grafana.

Coder organization/role IdP sync only manages org-scoped roles; the site-wide
Owner role is not claim-driven, so it is assigned explicitly here. Site roles are
not overwritten by the per-org IdP sync, so this persists across logins.

Idempotent: re-running is a no-op if the user already has Owner. Targets the demo
Coder explicitly (NOT the ambient $CODER_URL). Admin creds come from
~/.config/<CLUSTER_NAME>/generated-secrets.env.

Usage:
    python3 scripts/grant-coder-owner.py [username]   # default: austen.platform
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("DEMO_CODER_URL", "https://dev.<BASE_DOMAIN>").rstrip("/")
USERNAME = sys.argv[1] if len(sys.argv) > 1 else "austen.platform"
EMAIL_DOMAIN = "<BASE_DOMAIN>"


def creds():
    out = {}
    path = os.path.expanduser("~/.config/<CLUSTER_NAME>/generated-secrets.env")
    with open(path) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k] = v
    return out


C = creds()


def login():
    body = json.dumps({"email": C["CODER_ADMIN_EMAIL"],
                       "password": C["CODER_ADMIN_PASSWORD"]}).encode()
    req = urllib.request.Request(BASE + "/api/v2/users/login", data=body,
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))["session_token"]


TOKEN = None


def api(method, path, body=None):
    headers = {"Coder-Session-Token": TOKEN, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req)
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    global TOKEN
    TOKEN = login()
    # Coder sanitizes usernames (e.g. drops dots), so resolve by email.
    email = f"{USERNAME}@{EMAIL_DOMAIN}"
    code, res = api("GET", "/api/v2/users?q=" + email)
    user = None
    if code == 200:
        for u in (res.get("users") or []):
            if u.get("email", "").lower() == email.lower():
                user = u
                break
    if user is None:
        print(f"user {email}: not found (must SSO-login to Coder once first)",
              file=sys.stderr)
        sys.exit(1)
    roles = sorted({r["name"] if isinstance(r, dict) else r
                    for r in (user.get("roles") or [])})
    if "owner" in roles:
        print(f"{user['username']} ({email}): already site Owner ({user['id']})")
        return
    code, res = api("PUT", f"/api/v2/users/{user['id']}/roles", {"roles": ["owner"]})
    if code != 200:
        print(f"{email}: grant failed ({code}) {res}", file=sys.stderr)
        sys.exit(1)
    new = sorted({r["name"] if isinstance(r, dict) else r
                  for r in (res.get("roles") or [])})
    print(f"{user['username']} ({email}): site roles -> {new}")


if __name__ == "__main__":
    main()
