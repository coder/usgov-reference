#!/usr/bin/env python3
"""
setup-kiali-oidc.py - register the Kiali OIDC client in the Keycloak realm
`coder` so the Kiali mesh console logs in with the same SSO as Coder, Grafana,
and GitLab, and publish the client secret to AWS Secrets Manager for ESO to sync.

Idempotent: re-running ensures the desired client + full-path `groups` mapper and
upserts the secret. It does NOT rotate the Keycloak client secret on each run; it
reads the current secret and writes that value to ASM.

What it does:
  1. Create/update a confidential OIDC client `kiali` (standard authorization-code
     flow, PKCE S256, redirect URIs https://kiali.<BASE_DOMAIN>/kiali/* and
     the bare https://kiali.<BASE_DOMAIN>/kiali). Kiali's callback lives
     under its web_root (/kiali), so the wildcard covers
     /kiali/api/auth/openid_redirect.
  2. Add the same full-path `groups` group-membership mapper the coder, grafana,
     and gitlab clients use. Kiali keys the logged-in identity off `sub` and the
     `preferred_username` claim (delivered by Keycloak's default `profile` client
     scope); the `groups` claim is added for parity and future group-based authz.
  3. Read the client secret and upsert it into AWS Secrets Manager at
     <CLUSTER_NAME>/observability/kiali-oauth as {"oidc-secret": "...",
     "signing-key": "..."}.

The ASM JSON keys are intentionally `oidc-secret` (not `client-secret` as for
grafana) and `signing-key`: Kiali v2.26 reads the OIDC client secret from a
Secret named `kiali` (ns istio-system) under the key `oidc-secret`, and reads the
session token signing key from the same Secret under `signing-key` (referenced by
login_token.signing_key="secret:kiali:signing-key"). The kiali-oauth
ExternalSecret uses dataFrom.extract, so each ASM JSON key becomes the Kubernetes
Secret key verbatim, producing exactly the keys Kiali expects. The signing key is
preserved across re-runs; it is only generated when absent or not 16/24/32 bytes.

Reads admin credentials from ~/.config/<CLUSTER_NAME>/generated-secrets.env:
  KEYCLOAK_ADMIN_USERNAME, KEYCLOAK_ADMIN_PASSWORD

Pairs with deploy/istio/observability/kiali-server-values.yaml (auth.openid
config) and the kiali-oauth ExternalSecret in
deploy/istio/observability/externalsecret-kiali-oauth.yaml.
"""
import json
import os
import secrets
import string
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

KC = os.environ.get("KEYCLOAK_URL", "https://auth.<BASE_DOMAIN>").rstrip("/")
REALM = "coder"
CLIENT_ID = "kiali"
KIALI_URL = "https://kiali.<BASE_DOMAIN>"
REGION = "us-gov-west-1"
ASM_NAME = "<CLUSTER_NAME>/observability/kiali-oauth"

DESIRED_CLIENT = {
    "clientId": CLIENT_ID,
    "name": "Kiali",
    "description": "Kiali (Istio mesh console) SSO via Keycloak realm coder.",
    "enabled": True,
    "protocol": "openid-connect",
    "publicClient": False,
    "standardFlowEnabled": True,
    "implicitFlowEnabled": False,
    "directAccessGrantsEnabled": False,
    "serviceAccountsEnabled": False,
    "clientAuthenticatorType": "client-secret",
    "rootUrl": KIALI_URL,
    "baseUrl": "/kiali",
    "redirectUris": [KIALI_URL + "/kiali/*", KIALI_URL + "/kiali"],
    "webOrigins": [KIALI_URL],
    "attributes": {
        "pkce.code.challenge.method": "S256",
        "post.logout.redirect.uris": KIALI_URL + "/kiali/*",
    },
}

# Same full-path groups mapper the coder/grafana/gitlab clients use, so a future
# group-based policy can key off Keycloak group paths (e.g. /platform). Kiali
# does not enforce groups while disable_rbac is true, but emitting the claim now
# keeps the client consistent with the rest of the realm.
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
        # Merge attributes so we do not drop Keycloak-managed defaults.
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
    # No secret yet (should not happen for a confidential client); generate one.
    _, body = kc("POST", f"/clients/{cid}/client-secret")
    return body["value"]


def asm_exists(name):
    r = subprocess.run(
        ["aws", "secretsmanager", "describe-secret", "--region", REGION,
         "--secret-id", name],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.returncode == 0


def get_asm(name):
    r = subprocess.run(
        ["aws", "secretsmanager", "get-secret-value", "--region", REGION,
         "--secret-id", name, "--query", "SecretString", "--output", "text"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        return {}
    try:
        return json.loads(r.stdout.decode())
    except (ValueError, UnicodeDecodeError):
        return {}


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
             "--description", "<CLUSTER_NAME> Kiali OIDC client secret (ESO).",
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
    # The session token signing key must be 16, 24, or 32 bytes to keep Kiali on
    # the OIDC authorization-code flow. Preserve an existing valid key so re-runs
    # do not rotate it (which would invalidate live sessions); generate one only
    # when absent or malformed. kiali-server-values.yaml references this via
    # login_token.signing_key="secret:kiali:signing-key".
    existing = get_asm(ASM_NAME)
    signing_key = existing.get("signing-key", "")
    if len(signing_key) in (16, 24, 32):
        sk_action = "preserved"
    else:
        signing_key = "".join(
            secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
        sk_action = "generated"
    action = put_asm(ASM_NAME, {"oidc-secret": secret, "signing-key": signing_key})
    print(f"ASM {ASM_NAME}: {action} (oidc-secret, {len(secret)} chars; "
          f"signing-key {sk_action}, {len(signing_key)} chars)")
    print("\nNext: kubectl apply the kiali-oauth ExternalSecret, apply kiali.yaml, "
          "then roll Kiali (kubectl -n istio-system rollout restart deploy/kiali).")


if __name__ == "__main__":
    main()
