#!/usr/bin/env python3
"""
setup-gitlab-oidc.py - register the GitLab OIDC client in the Keycloak realm
`coder` so GitLab logs in with the same SSO as Coder and Grafana, and publish the
client secret to AWS Secrets Manager for ESO to sync.

Idempotent: re-running ensures the desired client + full-path `groups` mapper and
upserts the secret. It does NOT rotate the Keycloak client secret on each run; it
reads the current secret and writes that value to ASM.

What it does:
  1. Create/update a confidential OIDC client `gitlab` (standard flow, PKCE
     S256, redirect URI
     https://gitlab.<BASE_DOMAIN>/users/auth/openid_connect/callback).
  2. Add the same full-path `groups` group-membership mapper the coder and
     grafana clients use, so GitLab can map group membership to the instance
     admin attribute (admin_groups).
  3. Read the client secret and upsert it into AWS Secrets Manager at
     <CLUSTER_NAME>/gitlab/oidc as {"client-secret": "..."}.

Reads admin credentials from ~/.config/<CLUSTER_NAME>/generated-secrets.env:
  KEYCLOAK_ADMIN_USERNAME, KEYCLOAK_ADMIN_PASSWORD

Pairs with deploy/gitlab/statefulset.yaml (the openid_connect omniauth block in
GITLAB_OMNIBUS_CONFIG) and the gitlab-oidc ExternalSecret in
deploy/platform/external-secrets/secretstore-and-externalsecrets.yaml.
"""
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

KC = os.environ.get("KEYCLOAK_URL", "https://auth.<BASE_DOMAIN>").rstrip("/")
REALM = "coder"
CLIENT_ID = "gitlab"
GITLAB_URL = "https://gitlab.<BASE_DOMAIN>"
REGION = "us-gov-west-1"
ASM_NAME = "<CLUSTER_NAME>/gitlab/oidc"

DESIRED_CLIENT = {
    "clientId": CLIENT_ID,
    "name": "GitLab",
    "description": "GitLab SCM SSO via Keycloak realm coder.",
    "enabled": True,
    "protocol": "openid-connect",
    "publicClient": False,
    "standardFlowEnabled": True,
    "implicitFlowEnabled": False,
    "directAccessGrantsEnabled": False,
    "serviceAccountsEnabled": False,
    "clientAuthenticatorType": "client-secret",
    "rootUrl": GITLAB_URL,
    "baseUrl": "/",
    "redirectUris": [GITLAB_URL + "/users/auth/openid_connect/callback"],
    "webOrigins": [GITLAB_URL],
    "attributes": {
        "pkce.code.challenge.method": "S256",
        "post.logout.redirect.uris": GITLAB_URL + "/*",
    },
}

# Same full-path groups mapper the coder/grafana clients use, so GitLab's
# admin_groups can key off Keycloak group paths (e.g. /platform/platform-admins).
GROUPS_MAPPER = {
    "name": "groups",
    "protocol": "openid-connect",
    "protocolMapper": "oidc-group-membership-mapper",
    "config": {
        "full.path": "true",
        "id.token.claim": "true",
        "access.token.claim": "true",
        "userinfo.token.claim": "true",
        "lightweight.claim": "false",
        "claim.name": "groups",
    },
}

TOKEN = None


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


def kc(method, path, body=None):
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


def ensure_client():
    _, clients = kc("GET", "/clients?clientId=" + CLIENT_ID)
    if clients:
        cid = clients[0]["id"]
        rep = dict(clients[0])
        rep.update(DESIRED_CLIENT)
        attrs = dict(clients[0].get("attributes") or {})
        attrs.update(DESIRED_CLIENT["attributes"])
        rep["attributes"] = attrs
        code, _ = kc("PUT", f"/clients/{cid}", rep)
        print(f"client '{CLIENT_ID}': updated (HTTP {code})")
    else:
        code, _ = kc("POST", "/clients", DESIRED_CLIENT)
        print(f"client '{CLIENT_ID}': CREATED (HTTP {code})")
        _, clients = kc("GET", "/clients?clientId=" + CLIENT_ID)
        cid = clients[0]["id"]
    return cid


def ensure_mapper(cid):
    _, mappers = kc("GET", f"/clients/{cid}/protocol-mappers/models")
    existing = {m["name"]: m for m in (mappers or [])}
    rep = dict(GROUPS_MAPPER)
    if "groups" in existing:
        rep["id"] = existing["groups"]["id"]
        code, _ = kc("PUT",
                     f"/clients/{cid}/protocol-mappers/models/{rep['id']}", rep)
        print(f"client mapper 'groups': updated (HTTP {code})")
    else:
        code, _ = kc("POST", f"/clients/{cid}/protocol-mappers/models", rep)
        print(f"client mapper 'groups': CREATED (HTTP {code})")


def client_secret(cid):
    _, body = kc("GET", f"/clients/{cid}/client-secret")
    if isinstance(body, dict) and body.get("value"):
        return body["value"]
    _, body = kc("POST", f"/clients/{cid}/client-secret")
    return body["value"]


def asm_exists(name):
    r = subprocess.run(
        ["aws", "secretsmanager", "describe-secret", "--region", REGION,
         "--secret-id", name],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.returncode == 0


def put_asm(name, payload):
    fd, path = tempfile.mkstemp(prefix="asm-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        ref = "file://" + path
        if asm_exists(name):
            subprocess.run(
                ["aws", "secretsmanager", "put-secret-value", "--region", REGION,
                 "--secret-id", name, "--secret-string", ref],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return "updated"
        subprocess.run(
            ["aws", "secretsmanager", "create-secret", "--region", REGION,
             "--name", name,
             "--description", "<CLUSTER_NAME> GitLab OIDC client secret (ESO).",
             "--secret-string", ref],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return "created"
    finally:
        os.unlink(path)


def main():
    global TOKEN
    TOKEN = token()
    cid = ensure_client()
    ensure_mapper(cid)
    secret = client_secret(cid)
    action = put_asm(ASM_NAME, {"client-secret": secret})
    print(f"ASM {ASM_NAME}: {action} (client-secret, {len(secret)} chars)")
    print("\nNext: kubectl apply the gitlab-oidc ExternalSecret, then apply the "
          "statefulset (GitLab reconfigure + restart takes a few minutes).")


if __name__ == "__main__":
    main()
