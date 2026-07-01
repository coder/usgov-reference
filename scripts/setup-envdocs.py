#!/usr/bin/env python3
"""
setup-envdocs.py - stand up the environment documentation site at
https://envdocs.<BASE_DOMAIN>: a static MkDocs Material site served by
nginx in-cluster, gated by oauth2-proxy against Keycloak realm `coder` (any
authenticated realm user, no group restriction).

Idempotent and safe to re-run. Mirrors scripts/setup-grafana-oidc.py for the
OIDC client + AWS Secrets Manager (ASM) + ESO pattern.

When run with no flags it performs, in order:
  1. Create/update a confidential OIDC client `envdocs` in realm `coder`
     (standard flow, PKCE S256, redirect https://envdocs.<BASE_DOMAIN>/
     oauth2/callback) plus the shared full-path `groups` mapper (for parity;
     NOT enforced, the gate allows any authenticated realm user).
  2. Read the client secret, preserve (or generate once) the oauth2-proxy
     cookie secret, and upsert ASM <CLUSTER_NAME>/envdocs/oauth as
     {"client-secret": "...", "cookie-secret": "..."}. The cookie secret is
     generated only if ASM does not already have one, so sessions survive
     re-runs.
  3. Mirror the three required images into private ECR with crane (GovCloud has
     no pull-through cache): nginx, mkdocs-material, oauth2-proxy. Existing tags
     are skipped.
  4. Generate the envdocs-site ConfigMap from docs/envdocs/ and apply the
     deploy/envdocs/ manifests (namespace, ExternalSecret, Deployments,
     Services, Ingresses).
  5. Upsert a Route53 ALIAS envdocs.<BASE_DOMAIN> -> the ingress-nginx NLB
     (more specific than the *.<BASE_DOMAIN> wildcard, which points at the
     Istio gateway), so envdocs traffic flows through ingress-nginx.

Flags:
  --plan         read-only: print the intended actions, mutate nothing.
  --skip-mirror  skip the ECR image mirror step.
  --skip-apply   skip the kubectl apply / ConfigMap step.
  --skip-dns     skip the Route53 alias step.

Reads admin credentials from ~/.config/<CLUSTER_NAME>/generated-secrets.env:
  KEYCLOAK_ADMIN_USERNAME, KEYCLOAK_ADMIN_PASSWORD

Requires: kubectl (KUBECONFIG set), aws CLI, crane (for the mirror step).
Pairs with deploy/envdocs/ and the docs sources under docs/envdocs/.
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

KC = os.environ.get("KEYCLOAK_URL", "https://auth.<BASE_DOMAIN>").rstrip("/")
REALM = "coder"
CLIENT_ID = "envdocs"
ENVDOCS_URL = "https://envdocs.<BASE_DOMAIN>"
REGION = "us-gov-west-1"
ASM_NAME = "<CLUSTER_NAME>/envdocs/oauth"
ROUTE53_ZONE_ID = "<ROUTE53_ZONE_ID>"
INGRESS_NGINX_NS = "ingress-nginx"
INGRESS_NGINX_SVC = "ingress-nginx-controller"
ENVDOCS_FQDN = "envdocs.<BASE_DOMAIN>"
DEFAULT_ACCOUNT_ID = "<ACCOUNT_ID>"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY_DIR = os.path.join(REPO_ROOT, "deploy", "envdocs")
MKDOCS_YML = os.path.join(REPO_ROOT, "docs", "envdocs", "mkdocs.yml")
DOCS_DIR = os.path.join(REPO_ROOT, "docs", "envdocs", "docs")

# Upstream image -> (ECR repo path, tag). Mapping matches scripts/mirror-images.sh.
IMAGES = [
    ("docker.io/library/nginx:1.27-alpine",
     "docker-hub/library/nginx", "1.27-alpine"),
    ("docker.io/squidfunk/mkdocs-material:9.7.6",
     "docker-hub/squidfunk/mkdocs-material", "9.7.6"),
    ("quay.io/oauth2-proxy/oauth2-proxy:v7.7.1",
     "quay/oauth2-proxy/oauth2-proxy", "v7.7.1"),
]

DESIRED_CLIENT = {
    "clientId": CLIENT_ID,
    "name": "Environment Docs",
    "description": "Environment documentation site (envdocs) SSO via Keycloak realm coder.",
    "enabled": True,
    "protocol": "openid-connect",
    "publicClient": False,
    "standardFlowEnabled": True,
    "implicitFlowEnabled": False,
    "directAccessGrantsEnabled": False,
    "serviceAccountsEnabled": False,
    "clientAuthenticatorType": "client-secret",
    "rootUrl": ENVDOCS_URL,
    "baseUrl": "/",
    "redirectUris": [ENVDOCS_URL + "/oauth2/callback"],
    "webOrigins": [ENVDOCS_URL],
    "attributes": {
        "pkce.code.challenge.method": "S256",
        "post.logout.redirect.uris": ENVDOCS_URL + "/*",
    },
}

# Same full-path groups mapper the coder/grafana/kiali clients use. The envdocs
# gate does NOT enforce groups (any authenticated realm user is allowed); the
# mapper is emitted only for parity with the rest of the realm.
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


def token():
    secrets = read_secrets()
    data = urllib.parse.urlencode({
        "grant_type": "password",
        "client_id": "admin-cli",
        "username": secrets["KEYCLOAK_ADMIN_USERNAME"],
        "password": secrets["KEYCLOAK_ADMIN_PASSWORD"],
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


# --- AWS helpers -----------------------------------------------------------

def aws_json(args):
    r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        return None
    out = r.stdout.decode().strip()
    return json.loads(out) if out else None


def account_id():
    data = aws_json(["aws", "sts", "get-caller-identity", "--output", "json"])
    return (data or {}).get("Account", DEFAULT_ACCOUNT_ID)


def ecr_registry():
    return f"{account_id()}.dkr.ecr.{REGION}.amazonaws.com"


def asm_get_json(name):
    # Returns the parsed JSON object stored in the ASM secret, or None if the
    # secret does not exist or is not JSON.
    r = subprocess.run(
        ["aws", "secretsmanager", "get-secret-value", "--region", REGION,
         "--secret-id", name, "--query", "SecretString", "--output", "text"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        return None
    raw = r.stdout.decode().strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


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
                ["aws", "secretsmanager", "put-secret-value", "--region",
                 REGION, "--secret-id", name, "--secret-string", ref],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return "updated"
        subprocess.run(
            ["aws", "secretsmanager", "create-secret", "--region", REGION,
             "--name", name,
             "--description", "<CLUSTER_NAME> envdocs OIDC client + oauth2-proxy cookie secret (ESO).",
             "--secret-string", ref],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return "created"
    finally:
        os.unlink(path)


def gen_cookie_secret():
    # 32 random bytes, urlsafe-base64; oauth2-proxy base64-decodes to a 32-byte
    # AES key, which is a valid cookie-secret length.
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


# --- image mirror (crane) --------------------------------------------------

def ecr_image_exists(repo, tag):
    r = subprocess.run(
        ["aws", "ecr", "describe-images", "--region", REGION,
         "--repository-name", repo, "--image-ids", f"imageTag={tag}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.returncode == 0


def ecr_ensure_repo(repo):
    r = subprocess.run(
        ["aws", "ecr", "describe-repositories", "--region", REGION,
         "--repository-names", repo],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode == 0:
        return
    subprocess.run(
        ["aws", "ecr", "create-repository", "--region", REGION,
         "--repository-name", repo, "--image-tag-mutability", "IMMUTABLE",
         "--image-scanning-configuration", "scanOnPush=true"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    print(f"  ECR repo created: {repo}")


def crane_login():
    pw = subprocess.run(
        ["aws", "ecr", "get-login-password", "--region", REGION],
        check=True, stdout=subprocess.PIPE).stdout
    subprocess.run(
        ["crane", "auth", "login", ecr_registry(), "-u", "AWS",
         "--password-stdin"], input=pw, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def mirror_images():
    if not _have("crane"):
        sys.exit("ERROR: crane not found in PATH (needed to mirror images).")
    reg = ecr_registry()
    crane_login()
    for upstream, repo, tag in IMAGES:
        dst = f"{reg}/{repo}:{tag}"
        ecr_ensure_repo(repo)
        if ecr_image_exists(repo, tag):
            print(f"  image present, skip: {repo}:{tag}")
            continue
        print(f"  mirror {upstream} -> {dst}")
        subprocess.run(["crane", "copy", upstream, dst], check=True)


def _have(cmd):
    from shutil import which
    return which(cmd) is not None


# --- kubectl apply ---------------------------------------------------------

def kubectl(args, **kw):
    return subprocess.run(["kubectl"] + args, **kw)


def apply_configmap():
    yaml_bytes = subprocess.run(
        ["kubectl", "create", "configmap", "envdocs-site", "-n", "envdocs",
         "--from-file=mkdocs.yml=" + MKDOCS_YML, "--from-file=" + DOCS_DIR,
         "--dry-run=client", "-o", "yaml"],
        check=True, stdout=subprocess.PIPE).stdout
    kubectl(["apply", "-f", "-"], input=yaml_bytes, check=True)
    print("ConfigMap envdocs-site: applied")


def wait_for_secret(ns, name, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = kubectl(["get", "secret", name, "-n", ns],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0:
            print(f"Secret {ns}/{name}: present")
            return True
        time.sleep(3)
    print(f"WARN: Secret {ns}/{name} not present after {timeout}s; "
          "oauth2-proxy will stay Pending until ESO syncs it.")
    return False


def apply_manifests():
    kubectl(["apply", "-f", os.path.join(DEPLOY_DIR, "namespace.yaml")],
            check=True)
    apply_configmap()
    kubectl(["apply", "-f", os.path.join(DEPLOY_DIR, "externalsecret.yaml")],
            check=True)
    wait_for_secret("envdocs", "envdocs-oauth")
    for f in ("deployment.yaml", "oauth2-proxy.yaml", "ingress.yaml"):
        kubectl(["apply", "-f", os.path.join(DEPLOY_DIR, f)], check=True)
    print("manifests applied")


# --- Route53 ---------------------------------------------------------------

def nlb_hostname():
    r = kubectl(["get", "svc", INGRESS_NGINX_SVC, "-n", INGRESS_NGINX_NS,
                 "-o", "jsonpath={.status.loadBalancer.ingress[0].hostname}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    host = r.stdout.decode().strip()
    if not host:
        sys.exit("ERROR: could not resolve the ingress-nginx NLB hostname.")
    return host


def nlb_zone_id(dns):
    data = aws_json(
        ["aws", "elbv2", "describe-load-balancers", "--region", REGION,
         "--query",
         f"LoadBalancers[?DNSName=='{dns}'].CanonicalHostedZoneId",
         "--output", "json"])
    if data:
        return data[0]
    sys.exit(f"ERROR: could not resolve CanonicalHostedZoneId for NLB {dns}.")


def upsert_route53(dns, zone_id):
    batch = {
        "Comment": "envdocs alias to ingress-nginx NLB (more specific than the wildcard)",
        "Changes": [{
            "Action": "UPSERT",
            "ResourceRecordSet": {
                "Name": ENVDOCS_FQDN + ".",
                "Type": "A",
                "AliasTarget": {
                    "HostedZoneId": zone_id,
                    "DNSName": dns + ".",
                    "EvaluateTargetHealth": False,
                },
            },
        }],
    }
    fd, path = tempfile.mkstemp(prefix="r53-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(batch, f)
        subprocess.run(
            ["aws", "route53", "change-resource-record-sets",
             "--hosted-zone-id", ROUTE53_ZONE_ID,
             "--change-batch", "file://" + path],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print(f"Route53: UPSERT {ENVDOCS_FQDN} -> {dns} (zone {zone_id})")
    finally:
        os.unlink(path)


# --- plan ------------------------------------------------------------------

def do_plan():
    print("PLAN (read-only; nothing will be changed)\n")
    reg = ecr_registry()
    print(f"ECR registry         : {reg}")
    print(f"Keycloak client      : {CLIENT_ID} (realm {REALM}) "
          f"redirect {ENVDOCS_URL}/oauth2/callback")
    print(f"ASM secret           : {ASM_NAME} "
          f"({'exists' if asm_exists(ASM_NAME) else 'absent, will create'})")
    print("\nImages to mirror:")
    for upstream, repo, tag in IMAGES:
        state = "present" if ecr_image_exists(repo, tag) else "MISSING (will copy)"
        print(f"  {upstream}\n     -> {reg}/{repo}:{tag}  [{state}]")
    print("\nManifests to apply (deploy/envdocs/):")
    for f in ("namespace.yaml", "externalsecret.yaml", "deployment.yaml",
              "oauth2-proxy.yaml", "ingress.yaml"):
        print(f"  {os.path.join(DEPLOY_DIR, f)}")
    print("  ConfigMap envdocs-site (generated from docs/envdocs/)")
    host = nlb_hostname()
    zid = nlb_zone_id(host)
    print(f"\nRoute53 alias        : {ENVDOCS_FQDN} -> {host} "
          f"(zone target {zid}, hosted zone {ROUTE53_ZONE_ID})")
    print("\nNo changes made (--plan).")


# --- main ------------------------------------------------------------------

def main():
    global TOKEN
    ap = argparse.ArgumentParser(description="Set up the envdocs site + OIDC gate.")
    ap.add_argument("--plan", action="store_true",
                    help="read-only: print intended actions, mutate nothing")
    ap.add_argument("--skip-mirror", action="store_true",
                    help="skip the ECR image mirror step")
    ap.add_argument("--skip-apply", action="store_true",
                    help="skip the kubectl apply / ConfigMap step")
    ap.add_argument("--skip-dns", action="store_true",
                    help="skip the Route53 alias step")
    args = ap.parse_args()

    if args.plan:
        do_plan()
        return

    TOKEN = token()
    cid = ensure_client()
    ensure_mapper(cid)
    secret = client_secret(cid)

    existing = asm_get_json(ASM_NAME) or {}
    cookie = existing.get("cookie-secret") or gen_cookie_secret()
    action = put_asm(ASM_NAME, {"client-secret": secret, "cookie-secret": cookie})
    print(f"ASM {ASM_NAME}: {action} "
          f"(client-secret {len(secret)} chars, cookie-secret {len(cookie)} chars)")

    if not args.skip_mirror:
        print("\nMirroring images into ECR...")
        mirror_images()

    if not args.skip_apply:
        print("\nApplying manifests...")
        apply_manifests()

    if not args.skip_dns:
        print("\nUpserting Route53 alias...")
        host = nlb_hostname()
        upsert_route53(host, nlb_zone_id(host))

    print("\nDone. PASS probe:")
    print("  curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\\n' "
          f"https://{ENVDOCS_FQDN}/   # expect 302 -> auth.<BASE_DOMAIN>")
    print("  Then sign in as a realm `coder` user; the site returns 200 and "
          "Mermaid diagrams render.")


if __name__ == "__main__":
    main()
