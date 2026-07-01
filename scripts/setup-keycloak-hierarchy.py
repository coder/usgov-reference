#!/usr/bin/env python3
"""
setup-keycloak-hierarchy.py - build the Keycloak realm `coder` group/user
hierarchy and the OIDC `groups` claim mapper that Coder IdP sync consumes.

Idempotent: re-running ensures the desired state (groups, the group-membership
protocol mapper on the `coder` client, persona users + memberships) without
duplicating anything.

Reads admin + demo-user credentials from
~/.config/<CLUSTER_NAME>/generated-secrets.env:
  KEYCLOAK_ADMIN_USERNAME, KEYCLOAK_ADMIN_PASSWORD, DEMO_USER_PASSWORD

The operator super admin (austen.platform) is additionally given the
webauthn-register and CONFIGURE_TOTP required actions, so Keycloak forces passkey
+ TOTP enrollment on its next sign in. The actions are only (re)applied while the
matching credential is still missing, so a reconcile never forces re-enrollment.

Pairs with scripts/setup-coder-idp-sync.py (the Coder side). The hierarchy is
documented in docs/as-built/45-idp-sync-personas.md.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error

KC = os.environ.get("KEYCLOAK_URL", "https://auth.<BASE_DOMAIN>").rstrip("/")
REALM = "coder"
CLIENT_ID = "coder"

# Group tree: top-level (org) -> subgroups (teams + role groups).
GROUP_TREE = {
    "platform": ["platform-admins", "sre", "org-admins", "template-admins"],
    "alpha": ["developers", "data-science", "security", "org-admins", "auditors"],
    "bravo": ["developers", "org-admins", "auditors"],
}

# Persona users -> full group paths they belong to.
USERS = {
    # Operator super admin (not a demo persona). Dedicated account for the demo
    # operator: a member of ALL tenant orgs (org-admin in each) plus the Coder
    # site Owner role, GitLab instance admin, and Grafana admin, so one Keycloak
    # login administers the whole stack. Uses its own SUPERADMIN_PASSWORD.
    "austen.platform": {
        "first": "Austen", "last": "Platform",
        "password_env": "SUPERADMIN_PASSWORD",
        # Force passkey + TOTP enrollment on first sign in (super admin account).
        "required_actions": ["webauthn-register", "CONFIGURE_TOTP"],
        "groups": [
            "/platform", "/platform/platform-admins", "/platform/org-admins",
            "/alpha", "/alpha/org-admins",
            "/bravo", "/bravo/org-admins",
        ],
    },
    "pat.platform": {
        "first": "Pat", "last": "Rivera",
        "groups": ["/platform", "/platform/platform-admins", "/platform/org-admins"],
    },
    "sky.sre": {
        "first": "Sky", "last": "Nguyen",
        "groups": ["/platform", "/platform/sre", "/platform/template-admins"],
    },
    "alex.admin": {
        "first": "Alex", "last": "Carter",
        "groups": ["/alpha", "/alpha/org-admins"],
    },
    "dana.dev": {
        "first": "Dana", "last": "Brooks",
        "groups": ["/alpha", "/alpha/developers"],
    },
    "quinn.data": {
        "first": "Quinn", "last": "Lee",
        "groups": ["/alpha", "/alpha/data-science"],
    },
    "morgan.isso": {
        "first": "Morgan", "last": "Diaz",
        "groups": ["/alpha", "/alpha/auditors", "/bravo", "/bravo/auditors"],
    },
    "riley.admin": {
        "first": "Riley", "last": "Fox",
        "groups": ["/bravo", "/bravo/org-admins"],
    },
    "jordan.dev": {
        "first": "Jordan", "last": "Kim",
        "groups": ["/bravo", "/bravo/developers"],
    },
}

EMAIL_DOMAIN = "<BASE_DOMAIN>"

# Maps a required action to the credential type it provisions. Used to enforce
# MFA enrollment only while the credential is still missing, so reconciles stay
# idempotent and never force a re-enrollment once the user is set up.
REQUIRED_ACTION_CREDENTIAL = {
    "CONFIGURE_TOTP": "otp",
    "webauthn-register": "webauthn",
    "webauthn-register-passwordless": "webauthn-passwordless",
}


def read_secrets():
    path = os.path.expanduser("~/.config/<CLUSTER_NAME>/generated-secrets.env")
    out = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k] = v
    return out


SECRETS = read_secrets()
TOKEN = None


def token():
    data = urllib.parse.urlencode({
        "grant_type": "password",
        "client_id": "admin-cli",
        "username": SECRETS["KEYCLOAK_ADMIN_USERNAME"],
        "password": SECRETS["KEYCLOAK_ADMIN_PASSWORD"],
    }).encode()
    req = urllib.request.Request(
        KC + "/realms/master/protocol/openid-connect/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    return json.load(urllib.request.urlopen(req))["access_token"]


def kc(method, path, body=None, ok=(200, 201, 204)):
    headers = {"Authorization": "Bearer " + TOKEN}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(KC + "/admin/realms/" + REALM + path,
                                 data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req)
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def ensure_groups():
    """Create the group tree if missing; return {full_path: id}."""
    # Top-level groups.
    _, tops = kc("GET", "/groups?max=200")
    top_by_name = {g["name"]: g for g in tops}
    for name in GROUP_TREE:
        if name not in top_by_name:
            code, _ = kc("POST", "/groups", {"name": name})
            print(f"group /{name}: CREATED (HTTP {code})")
    # Re-fetch to get ids.
    _, tops = kc("GET", "/groups?max=200")
    top_by_name = {g["name"]: g for g in tops}

    paths = {}
    for name, children in GROUP_TREE.items():
        top = top_by_name[name]
        paths["/" + name] = top["id"]
        _, existing = kc("GET", f"/groups/{top['id']}/children?max=200")
        child_by_name = {g["name"]: g for g in existing}
        for child in children:
            if child not in child_by_name:
                code, _ = kc("POST", f"/groups/{top['id']}/children", {"name": child})
                print(f"group /{name}/{child}: CREATED (HTTP {code})")
        _, existing = kc("GET", f"/groups/{top['id']}/children?max=200")
        for g in existing:
            paths[f"/{name}/{g['name']}"] = g["id"]
    return paths


def ensure_mapper():
    """Group-membership mapper on the coder client -> full-path `groups` claim."""
    _, clients = kc("GET", "/clients?clientId=" + CLIENT_ID)
    cid = clients[0]["id"]
    _, mappers = kc("GET", f"/clients/{cid}/protocol-mappers/models")
    existing = {m["name"]: m for m in (mappers or [])}
    desired_config = {
        "full.path": "true",
        "id.token.claim": "true",
        "access.token.claim": "true",
        "userinfo.token.claim": "true",
        "lightweight.claim": "false",
        "claim.name": "groups",
    }
    rep = {
        "name": "groups",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-group-membership-mapper",
        "config": desired_config,
    }
    if "groups" in existing:
        m = existing["groups"]
        rep["id"] = m["id"]
        code, _ = kc("PUT", f"/clients/{cid}/protocol-mappers/models/{m['id']}", rep)
        print(f"client mapper 'groups': updated (HTTP {code})")
    else:
        code, _ = kc("POST", f"/clients/{cid}/protocol-mappers/models", rep)
        print(f"client mapper 'groups': CREATED (HTTP {code})")


def ensure_users(paths):
    for username, spec in USERS.items():
        pw_env = spec.get("password_env")
        pw = SECRETS.get(pw_env) if pw_env else None
        if not pw:
            pw = SECRETS["DEMO_USER_PASSWORD"]
        _, found = kc("GET", "/users?exact=true&username=" + urllib.parse.quote(username))
        if found:
            uid = found[0]["id"]
            print(f"user {username}: exists")
        else:
            rep = {
                "username": username,
                "email": f"{username}@{EMAIL_DOMAIN}",
                "firstName": spec["first"],
                "lastName": spec["last"],
                "enabled": True,
                "emailVerified": True,
            }
            code, _ = kc("POST", "/users", rep)
            _, found = kc("GET", "/users?exact=true&username=" + urllib.parse.quote(username))
            uid = found[0]["id"]
            print(f"user {username}: CREATED (HTTP {code})")
        # Password (non-temporary so login is immediate).
        kc("PUT", f"/users/{uid}/reset-password",
           {"type": "password", "value": pw, "temporary": False})
        # Group memberships (PUT is idempotent).
        for gpath in spec["groups"]:
            gid = paths[gpath]
            code, _ = kc("PUT", f"/users/{uid}/groups/{gid}")
        print(f"  {username}: groups -> {', '.join(spec['groups'])}")
        ensure_required_actions(uid, username, spec.get("required_actions") or [])


def ensure_required_actions(uid, username, desired):
    """Add required actions that enforce MFA enrollment, but only while the
    matching credential is missing (so a reconcile never re-forces enrollment)."""
    if not desired:
        return
    _, creds = kc("GET", f"/users/{uid}/credentials")
    have = {c.get("type") for c in (creds or [])}
    pending = [a for a in desired
               if REQUIRED_ACTION_CREDENTIAL.get(a) not in have]
    _, rep = kc("GET", f"/users/{uid}")
    current = list(rep.get("requiredActions") or [])
    merged = current + [a for a in pending if a not in current]
    if merged != current:
        rep["requiredActions"] = merged
        kc("PUT", f"/users/{uid}", rep)
    print(f"  {username}: required actions -> {merged or '[]'}")


def main():
    global TOKEN
    TOKEN = token()
    paths = ensure_groups()
    ensure_mapper()
    ensure_users(paths)
    print("\nGroup paths:")
    for p in sorted(paths):
        print(" ", p)


if __name__ == "__main__":
    main()
