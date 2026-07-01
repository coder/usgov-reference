#!/usr/bin/env bash
# =============================================================================
# set-appearance.sh: set the Coder dashboard appearance (classification banner)
# =============================================================================
# The appearance config (announcement banners, app name, logo) is a RUNTIME
# setting stored in the Coder database, NOT in the Helm chart. This script
# reproduces it idempotently so the demo banner survives a fresh deploy.
#
# Requires the premium/Enterprise license (announcement banners are gated).
#
# Usage:
#   ./scripts/set-appearance.sh
#
# Env (with sane demo defaults):
#   DEMO_CODER_URL       default https://dev.<BASE_DOMAIN>
#   APP_NAME             default "USGOV Coder Demo"
#   BANNER_MESSAGE       default "UNCLASSIFIED - USGOVCLOUD"
#   BANNER_COLOR         default "#007a33"  (IC/DoD UNCLASSIFIED green)
# Admin creds are read from ~/.config/<CLUSTER_NAME>/generated-secrets.env.
#
# NOTE: This intentionally uses DEMO_CODER_URL, not CODER_URL. When this runs
# inside a Coder workspace, the agent already exports CODER_URL pointing at the
# HOST Coder (e.g. https://dev.coder.com); reusing it would target the wrong
# deployment.
set -euo pipefail

export CODER_URL="${DEMO_CODER_URL:-https://dev.<BASE_DOMAIN>}"
export APP_NAME="${APP_NAME:-USGOV Coder Demo}"
export BANNER_MESSAGE="${BANNER_MESSAGE:-UNCLASSIFIED - USGOVCLOUD}"
export BANNER_COLOR="${BANNER_COLOR:-#007a33}"
SECRETS="${HOME}/.config/<CLUSTER_NAME>/generated-secrets.env"

# shellcheck disable=SC1090
. "${SECRETS}"
export CODER_ADMIN_EMAIL CODER_ADMIN_PASSWORD

# Login and PUT the appearance in one Python pass: avoids shell JSON quoting
# bugs and is resilient to special characters in the banner or credentials.
python3 - <<'PY'
import json, os, urllib.request, urllib.error, sys

base = os.environ["CODER_URL"].rstrip("/")


def call(method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Coder-Session-Token"] = token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req)
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        print(f"FAILED: {method} {path} -> {e.code} {e.read().decode()[:300]}", file=sys.stderr)
        sys.exit(1)


_, login = call("POST", "/api/v2/users/login", {
    "email": os.environ["CODER_ADMIN_EMAIL"],
    "password": os.environ["CODER_ADMIN_PASSWORD"],
})
token = login["session_token"]

call("PUT", "/api/v2/appearance", {
    "application_name": os.environ["APP_NAME"],
    "logo_url": "",
    "service_banner": {"enabled": False},
    "announcement_banners": [{
        "enabled": True,
        "message": os.environ["BANNER_MESSAGE"],
        "background_color": os.environ["BANNER_COLOR"],
    }],
}, token=token)

status, appearance = call("GET", "/api/v2/appearance", token=token)
print("application_name:", json.dumps(appearance.get("application_name")))
print("appearance set:", json.dumps(appearance["announcement_banners"]))
PY
