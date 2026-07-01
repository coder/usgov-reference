#!/usr/bin/env python3
"""
setup-coder-idp-sync.py - configure Coder organizations, groups, and OIDC IdP
sync (organizations + groups + roles) for the GovCloud multi-tenant demo.

Idempotent: safe to re-run. Discovers existing orgs/groups by name and only
creates what is missing, then PATCHes the sync settings to the desired state.

Targets the demo Coder explicitly (NOT the ambient $CODER_URL, which inside a
Coder workspace points at the host Coder). Admin creds come from
~/.config/<CLUSTER_NAME>/generated-secrets.env.

Usage:
    python3 scripts/setup-coder-idp-sync.py

The Keycloak side (groups, group-membership mapper, persona users) is created
by scripts/setup-keycloak-hierarchy.py. Both read from the same hierarchy
described in docs/as-built/45-idp-sync-personas.md.
"""
import json
import os
import sys
import urllib.request
import urllib.error

BASE = os.environ.get("DEMO_CODER_URL", "https://dev.<BASE_DOMAIN>").rstrip("/")

# --- Desired hierarchy -------------------------------------------------------
# Organizations: slug -> display name. "coder" is the pre-existing default org.
ORGS = {
    "coder": "Platform Engineering",
    "alpha": "Mission Partner Alpha",
    "bravo": "Mission Partner Bravo",
}

# Coder groups to pre-create per org slug (do not rely on auto-create).
GROUPS = {
    "coder": ["platform-admins", "sre"],
    "alpha": ["developers", "data-science", "security"],
    "bravo": ["developers"],
}

# Organization sync (deployment-level): full Keycloak group path -> org slug.
ORG_SYNC = {
    "/platform": "coder",
    "/alpha": "alpha",
    "/bravo": "bravo",
}

# Group sync per org slug: Keycloak group path -> Coder group name (in that org).
GROUP_SYNC = {
    "coder": {
        "/platform/platform-admins": "platform-admins",
        "/platform/sre": "sre",
    },
    "alpha": {
        "/alpha/developers": "developers",
        "/alpha/data-science": "data-science",
        "/alpha/security": "security",
    },
    "bravo": {
        "/bravo/developers": "developers",
    },
}

# Role sync per org slug: Keycloak group path -> list of Coder org role names.
ROLE_SYNC = {
    "coder": {
        "/platform/org-admins": ["organization-admin"],
        "/platform/template-admins": ["organization-template-admin"],
    },
    "alpha": {
        "/alpha/org-admins": ["organization-admin"],
        "/alpha/auditors": ["organization-auditor"],
    },
    "bravo": {
        "/bravo/org-admins": ["organization-admin"],
        "/bravo/auditors": ["organization-auditor"],
    },
}


def login():
    secrets = os.path.expanduser("~/.config/<CLUSTER_NAME>/generated-secrets.env")
    creds = {}
    with open(secrets) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                creds[k] = v
    body = json.dumps({
        "email": creds["CODER_ADMIN_EMAIL"],
        "password": creds["CODER_ADMIN_PASSWORD"],
    }).encode()
    req = urllib.request.Request(BASE + "/api/v2/users/login", data=body,
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))["session_token"]


TOKEN = None


def api(method, path, body=None, ok=(200, 201)):
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

    # 1. Organizations -------------------------------------------------------
    _, orgs = api("GET", "/api/v2/organizations")
    by_slug = {o["name"]: o for o in orgs}
    org_id = {}
    for slug, display in ORGS.items():
        if slug in by_slug:
            o = by_slug[slug]
            org_id[slug] = o["id"]
            if o.get("display_name") != display:
                code, _ = api("PATCH", f"/api/v2/organizations/{o['id']}",
                              {"display_name": display})
                print(f"org {slug}: display_name -> {display!r} (HTTP {code})")
            else:
                print(f"org {slug}: exists ({o['id']})")
        else:
            code, o = api("POST", "/api/v2/organizations",
                          {"name": slug, "display_name": display})
            if code not in (200, 201):
                print(f"FAILED creating org {slug}: {code} {o}", file=sys.stderr)
                sys.exit(1)
            org_id[slug] = o["id"]
            print(f"org {slug}: CREATED ({o['id']})")

    # 2. Groups (pre-create) -------------------------------------------------
    group_id = {}  # (slug, name) -> id
    for slug, names in GROUPS.items():
        _, existing = api("GET", f"/api/v2/organizations/{org_id[slug]}/groups")
        ex = {g["name"]: g["id"] for g in existing}
        for name in names:
            if name in ex:
                group_id[(slug, name)] = ex[name]
                print(f"group {slug}/{name}: exists")
            else:
                code, g = api("POST", f"/api/v2/organizations/{org_id[slug]}/groups",
                              {"name": name, "display_name": name})
                if code not in (200, 201):
                    print(f"FAILED group {slug}/{name}: {code} {g}", file=sys.stderr)
                    sys.exit(1)
                group_id[(slug, name)] = g["id"]
                print(f"group {slug}/{name}: CREATED")

    # 3. Organization sync (deployment-level) --------------------------------
    org_mapping = {path: [org_id[slug]] for path, slug in ORG_SYNC.items()}
    code, _ = api("PATCH", "/api/v2/settings/idpsync/organization", {
        "field": "groups",
        "mapping": org_mapping,
        "organization_assign_default": False,
    })
    print(f"org-sync: field=groups assign_default=false (HTTP {code})")

    # 4. Group sync (per org) ------------------------------------------------
    for slug, mapping in GROUP_SYNC.items():
        m = {path: [group_id[(slug, name)]] for path, name in mapping.items()}
        code, _ = api("PATCH",
                      f"/api/v2/organizations/{org_id[slug]}/settings/idpsync/groups", {
                          "field": "groups",
                          "mapping": m,
                          "regex_filter": None,
                          "auto_create_missing_groups": False,
                      })
        print(f"group-sync[{slug}]: {len(m)} mappings (HTTP {code})")

    # 5. Role sync (per org) -------------------------------------------------
    for slug, mapping in ROLE_SYNC.items():
        code, _ = api("PATCH",
                      f"/api/v2/organizations/{org_id[slug]}/settings/idpsync/roles", {
                          "field": "groups",
                          "mapping": mapping,
                      })
        print(f"role-sync[{slug}]: {len(mapping)} mappings (HTTP {code})")

    print("\nOrg IDs:", json.dumps(org_id))


if __name__ == "__main__":
    main()
