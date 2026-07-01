#!/usr/bin/env python3
"""
verify-drift.py - read-only drift diagnostic for the <CLUSTER_NAME> GovCloud
demo. It compares the intended source of truth (AWS Secrets Manager) against
the live Kubernetes Secrets and against git, and runs a few app-level Coder
checks, reporting PASS / WARN / FAIL per check.

This script is STRICTLY READ-ONLY. It only reads:

  * Kubernetes objects (kubectl get ... -o json), never apply/set/delete.
  * AWS Secrets Manager values (get-secret-value), never put/update/create.
  * The Coder API (login, GET deployment/config, GET token list, and a single
    AI Gateway routing probe), never any write to platform state.

It NEVER prints a secret value. For secret material it prints only a sha256
fingerprint truncated to 12 hex chars, the decoded byte length, and a pass or
fail verdict. The Coder admin password is read from
~/.config/<CLUSTER_NAME>/generated-secrets.env and used only to obtain a
session token; it is never logged.

Check groups:

  1. eso-sync     ExternalSecret sync condition (Ready / SecretSynced).
  2. drift        ASM-vs-k8s fingerprint match per Secret data key.
  3. placeholder  placeholder material satisfying an "exists" check, plus a
                  shape check for the Anthropic API key.
  4. token-life   CODER_MAX_TOKEN_LIFETIME / CODER_MAX_ADMIN_TOKEN_LIFETIME
                  agreement across git, live deployment env, and the API.
  5. ci-token     freshness of the gitlab-ci Coder API token.
  6. ai-health    Anthropic AI Gateway route reachability (best-effort).

Usage (from the repo root, with the demo kubeconfig + env):
    set -a; . ~/.config/<CLUSTER_NAME>/env; \\
        . ~/.config/<CLUSTER_NAME>/generated-secrets.env; set +a
    export KUBECONFIG=$WORKSPACE_ROOT/<CLUSTER_NAME>/kubeconfig
    export PATH="$HOME/.local/bin:$PATH"
    python3 scripts/verify-drift.py            # fixed-width table
    python3 scripts/verify-drift.py --json     # JSON array (for CI)

Exit code is 0 when there are no FAIL rows (WARN is allowed), else 1.
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

# --- Configuration -----------------------------------------------------------
CODER_URL = "https://dev.<BASE_DOMAIN>"
REGION = "us-gov-west-1"

SECRETS_ENV = os.path.expanduser(
    "~/.config/<CLUSTER_NAME>/generated-secrets.env")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODER_VALUES = os.path.join(REPO_ROOT, "deploy", "coder", "values.yaml")

# Token lifetime that git, the live deployment, and the API must all agree on.
EXPECTED_LIFETIME = "8760h"          # 365 days.
NS_PER_HOUR = 3600 * 1_000_000_000

CI_TOKEN_NAME = "gitlab-ci"
CI_TOKEN_WARN_DAYS = 30

# The Anthropic key lives in k8s Secret coder/coder-ai under this data key, and
# in ASM secret <CLUSTER_NAME>/coder/ai under the same JSON field.
ANTHROPIC_NS = "coder"
ANTHROPIC_SECRET = "coder-ai"
ANTHROPIC_KEY = "ANTHROPIC_API_KEY"
ANTHROPIC_MIN_LEN = 60
ANTHROPIC_PREFIX = "sk-ant-"

# Placeholder markers that must never satisfy an "exists" check.
PLACEHOLDER_MARKERS = ("replace", "placeholder", "changeme", "replaced_by_eso")

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


# --- Result accumulator ------------------------------------------------------
class Results:
    """Collects one row per check and tracks the worst overall verdict."""

    def __init__(self):
        self.rows = []

    def add(self, group, check, status, detail):
        self.rows.append({"group": group, "check": check,
                          "status": status, "detail": detail})

    def counts(self):
        c = {PASS: 0, WARN: 0, FAIL: 0}
        for r in self.rows:
            c[r["status"]] = c.get(r["status"], 0) + 1
        return c


# --- Secret material helpers (never print the value) -------------------------
def fingerprint(raw_bytes):
    """Return a 12-hex sha256 fingerprint of the given bytes."""
    return hashlib.sha256(raw_bytes).hexdigest()[:12]


def asm_field_bytes(value):
    """Return the byte representation ESO would sync for an ASM JSON field. A
    string field is stored verbatim; a structured field is JSON-encoded."""
    if isinstance(value, str):
        return value.encode()
    return json.dumps(value, separators=(",", ":")).encode()


def contains_placeholder(raw_bytes):
    """Return the placeholder marker found in the value, or None."""
    text = raw_bytes.decode("utf-8", "ignore").lower()
    for marker in PLACEHOLDER_MARKERS:
        if marker in text:
            return marker
    return None


# --- generated-secrets.env ---------------------------------------------------
def read_secret(*keys):
    """Read selected keys from generated-secrets.env without echoing values."""
    out = {}
    with open(SECRETS_ENV) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                if k in keys:
                    out[k] = v.strip().strip('"').strip("'")
    return out


# --- Coder API ---------------------------------------------------------------
def coder_request(method, path, token=None, body=None, extra_headers=None):
    """Issue a Coder API request. Returns (status, parsed_or_text, error)."""
    headers = {}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if token:
        headers["Coder-Session-Token"] = token
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(CODER_URL + path, data=data,
                                 headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req)
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw else None), None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), None
    except urllib.error.URLError as e:
        return None, None, str(e.reason)


def coder_login(email, password):
    """Log in and return a session token, or None on failure."""
    status, body, _ = coder_request(
        "POST", "/api/v2/users/login",
        body={"email": email, "password": password})
    if status == 201 and isinstance(body, dict):
        return body.get("session_token")
    return None


# --- subprocess helpers ------------------------------------------------------
def run_json(args):
    """Run a command and parse stdout as JSON. Returns (obj, error_str)."""
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout or "command failed").strip()
    try:
        return json.loads(r.stdout), None
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e}"


def kubectl_get_json(*args):
    return run_json(["kubectl", "get", *args, "-o", "json"])


def asm_get(secret_id):
    """Fetch and parse an ASM secret's SecretString. Returns (dict, error)."""
    r = subprocess.run(
        ["aws", "secretsmanager", "get-secret-value", "--secret-id", secret_id,
         "--region", REGION, "--query", "SecretString", "--output", "text"],
        capture_output=True, text=True)
    if r.returncode != 0:
        return None, (r.stderr or "aws error").strip().splitlines()[-1]
    try:
        obj = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None, "ASM SecretString is not JSON"
    if not isinstance(obj, dict):
        return None, "ASM SecretString is not a JSON object"
    return obj, None


# --- Check group 1 + 2: ESO sync and ASM-vs-k8s fingerprint ------------------
def decode_k8s_secret(secret_obj):
    """Return {key: raw_bytes} for a Kubernetes Secret object."""
    out = {}
    for k, v in (secret_obj.get("data") or {}).items():
        try:
            out[k] = base64.b64decode(v)
        except (ValueError, TypeError):
            out[k] = b""
    return out


def es_ready(status):
    """Return (ok, reason) for an ExternalSecret status block."""
    for cond in status.get("conditions", []):
        if cond.get("type") == "Ready":
            return cond.get("status") == "True", cond.get("reason", "?")
    return False, "NoReadyCondition"


def asm_map_for_es(spec):
    """Resolve the ASM-backed value for each target k8s key.

    Returns (mapping, errors) where mapping is {k8s_key: asm_value_bytes} and
    errors is a list of human-readable ASM resolution problems.
    """
    mapping = {}
    errors = []
    asm_cache = {}

    def load(asm_id):
        if asm_id not in asm_cache:
            asm_cache[asm_id] = asm_get(asm_id)
        return asm_cache[asm_id]

    # dataFrom[].extract.key: every ASM JSON field maps 1:1 to a k8s key.
    for entry in spec.get("dataFrom", []) or []:
        asm_id = (entry.get("extract") or {}).get("key")
        if not asm_id:
            continue
        obj, err = load(asm_id)
        if err:
            errors.append(f"{asm_id}: {err}")
            continue
        for field, value in obj.items():
            mapping[field] = asm_field_bytes(value)

    # data[].remoteRef: a single ASM field (optionally .property) -> one key.
    for entry in spec.get("data", []) or []:
        ref = entry.get("remoteRef") or {}
        asm_id = ref.get("key")
        prop = ref.get("property")
        k8s_key = entry.get("secretKey")
        if not asm_id or not k8s_key:
            continue
        obj, err = load(asm_id)
        if err:
            errors.append(f"{asm_id}: {err}")
            continue
        if prop:
            if prop in obj:
                mapping[k8s_key] = asm_field_bytes(obj[prop])
            else:
                errors.append(f"{asm_id}: missing property {prop}")
        elif len(obj) == 1:
            mapping[k8s_key] = asm_field_bytes(next(iter(obj.values())))
        else:
            errors.append(f"{asm_id}: ambiguous remoteRef without property")
    return mapping, errors


def check_external_secrets(results):
    """Run eso-sync and drift checks. Returns the fetched k8s secrets keyed by
    "namespace/name" -> {data_key: raw_bytes} for reuse by later groups."""
    fetched = {}
    obj, err = kubectl_get_json("externalsecrets.external-secrets.io", "-A")
    if err:
        results.add("eso-sync", "list", FAIL, f"kubectl error: {err}")
        return fetched
    items = obj.get("items", [])
    if not items:
        results.add("eso-sync", "list", WARN, "no ExternalSecrets found")
        return fetched

    for es in sorted(items, key=lambda i: (i["metadata"]["namespace"],
                                           i["metadata"]["name"])):
        md = es["metadata"]
        spec = es.get("spec", {})
        ns = md["namespace"]
        name = md["name"]
        label = f"{ns}/{name}"

        ok, reason = es_ready(es.get("status", {}))
        results.add("eso-sync", label, PASS if ok else FAIL,
                    f"Ready={ok} reason={reason}")

        target = (spec.get("target") or {}).get("name") or name
        sec_obj, serr = kubectl_get_json("secret", target, "-n", ns)
        if serr:
            results.add("drift", f"{ns}/{target}", WARN,
                        f"k8s secret unreadable: {serr}")
            continue
        k8s = decode_k8s_secret(sec_obj)
        fetched[f"{ns}/{target}"] = k8s

        mapping, asm_errors = asm_map_for_es(spec)
        matches, mismatches, unmapped = [], [], []
        for key, raw in sorted(k8s.items()):
            kfp = fingerprint(raw)
            if key in mapping:
                afp = fingerprint(mapping[key])
                if afp == kfp:
                    matches.append(f"{key}={kfp}/{len(raw)}b")
                else:
                    mismatches.append(f"{key} k8s={kfp} asm={afp}")
            else:
                unmapped.append(f"{key}={kfp}/{len(raw)}b")

        if mismatches:
            status = FAIL
            detail = "MISMATCH " + "; ".join(mismatches)
        elif asm_errors:
            status = WARN
            detail = "ASM unresolved: " + "; ".join(asm_errors)
        elif unmapped and not matches:
            status = WARN
            detail = "UNMAPPED " + ", ".join(unmapped)
        else:
            status = PASS
            detail = f"{len(matches)} key(s) match"
            if unmapped:
                detail += f", {len(unmapped)} unmapped"
        results.add("drift", f"{ns}/{target}", status, detail)
    return fetched


# --- Check group 2: placeholder detection ------------------------------------
def check_placeholders(results, fetched):
    """FAIL any secret whose decoded value contains a placeholder marker, then
    run the Anthropic key shape check."""
    for label in sorted(fetched):
        hits = []
        for key, raw in fetched[label].items():
            marker = contains_placeholder(raw)
            if marker:
                hits.append(f"{key} contains '{marker}'")
        if hits:
            results.add("placeholder", label, FAIL, "; ".join(hits))
        else:
            results.add("placeholder", label, PASS,
                        f"{len(fetched[label])} key(s) clean")

    # Anthropic key shape: length and prefix only, never the value.
    label = f"{ANTHROPIC_NS}/{ANTHROPIC_SECRET} {ANTHROPIC_KEY}"
    secret = fetched.get(f"{ANTHROPIC_NS}/{ANTHROPIC_SECRET}")
    if not secret or ANTHROPIC_KEY not in secret:
        results.add("placeholder", label, FAIL, "key not present")
        return
    raw = secret[ANTHROPIC_KEY]
    text = raw.decode("utf-8", "ignore")
    good_prefix = text.startswith(ANTHROPIC_PREFIX)
    good_len = len(raw) >= ANTHROPIC_MIN_LEN
    status = PASS if (good_prefix and good_len) else FAIL
    results.add("placeholder", label, status,
                f"len={len(raw)} prefix_ok={good_prefix}")


# --- Check group 3: token-lifetime consistency -------------------------------
def parse_go_duration_ns(text):
    """Parse a simple Go-style duration of h/m/s units into nanoseconds.
    Returns None when the value cannot be parsed."""
    if not text:
        return None
    total = 0
    units = {"h": 3600, "m": 60, "s": 1}
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)([hms])", text):
        total += float(value) * units[unit]
    if total == 0:
        return None
    return int(total * 1_000_000_000)


def git_values_env(names):
    """Extract `value:` fields for the given env `name:` entries from the Coder
    Helm values.yaml. Returns {name: value}."""
    out = {}
    try:
        with open(CODER_VALUES) as f:
            lines = f.readlines()
    except OSError:
        return out
    for i, line in enumerate(lines):
        m = re.match(r"\s*-\s*name:\s*(\S+)", line)
        if not m or m.group(1) not in names:
            continue
        for nxt in lines[i + 1:i + 4]:
            vm = re.match(r"\s*value:\s*(.+)", nxt)
            if vm:
                out[m.group(1)] = vm.group(1).strip().strip('"').strip("'")
                break
    return out


def live_deployment_env(names):
    """Read the given env values from the live coder Deployment. Returns
    ({name: value}, error)."""
    obj, err = kubectl_get_json("deployment", "coder", "-n", "coder")
    if err:
        return {}, err
    out = {}
    containers = obj["spec"]["template"]["spec"]["containers"]
    for c in containers:
        for e in c.get("env", []):
            if e.get("name") in names and "value" in e:
                out[e["name"]] = e["value"]
    return out, None


def check_token_lifetime(results, session):
    """PASS only when git, live env, and the API agree on each lifetime."""
    names = ["CODER_MAX_TOKEN_LIFETIME", "CODER_MAX_ADMIN_TOKEN_LIFETIME"]
    git = git_values_env(names)
    live, live_err = live_deployment_env(names)

    api = {}
    api_err = None
    status, cfg, neterr = coder_request(
        "GET", "/api/v2/deployment/config", token=session)
    if neterr or status != 200 or not isinstance(cfg, dict):
        api_err = neterr or f"HTTP {status}"
    else:
        sl = (cfg.get("config") or {}).get("session_lifetime") or {}
        api["CODER_MAX_TOKEN_LIFETIME"] = sl.get("max_token_lifetime")
        api["CODER_MAX_ADMIN_TOKEN_LIFETIME"] = sl.get("max_admin_token_lifetime")

    expected_ns = parse_go_duration_ns(EXPECTED_LIFETIME)
    for name in names:
        gval = git.get(name)
        lval = live.get(name)
        aval = api.get(name)
        details = [f"git={gval}", f"live={lval}", f"api={aval}ns"]
        if live_err:
            results.add("token-life", name, WARN,
                        f"live unreadable: {live_err}; git={gval}")
            continue
        if api_err:
            results.add("token-life", name, WARN,
                        f"api unreadable: {api_err}; git={gval} live={lval}")
            continue
        gns = parse_go_duration_ns(gval)
        lns = parse_go_duration_ns(lval)
        try:
            ans = int(aval)
        except (TypeError, ValueError):
            ans = None
        agree = (gns is not None and gns == lns == ans == expected_ns
                 and gval == lval == EXPECTED_LIFETIME)
        results.add("token-life", name, PASS if agree else FAIL,
                    " ".join(details) + f" expect={EXPECTED_LIFETIME}")


# --- Check group 4: gitlab-ci token freshness --------------------------------
def parse_iso(ts):
    if not ts:
        return None
    ts = ts.replace("Z", "+00:00")
    # Trim fractional seconds to microsecond precision for fromisoformat.
    m = re.match(r"(.*\.\d{1,6})\d*([+-]\d\d:\d\d)$", ts)
    if m:
        ts = m.group(1) + m.group(2)
    try:
        return datetime.datetime.fromisoformat(ts)
    except ValueError:
        return None


def check_ci_token(results, session):
    status, tokens, neterr = coder_request(
        "GET", "/api/v2/users/me/keys/tokens", token=session)
    if neterr or status != 200 or not isinstance(tokens, list):
        results.add("ci-token", CI_TOKEN_NAME, WARN,
                    f"token list unreadable: {neterr or ('HTTP %s' % status)}")
        return
    match = next((t for t in tokens
                  if t.get("token_name") == CI_TOKEN_NAME), None)
    if not match:
        results.add("ci-token", CI_TOKEN_NAME, FAIL, "token not found")
        return
    expires = parse_iso(match.get("expires_at"))
    created = match.get("created_at", "?")[:10]
    exp_str = match.get("expires_at", "?")[:10]
    tid = (match.get("id") or "")[:8]
    if expires is None:
        results.add("ci-token", CI_TOKEN_NAME, WARN,
                    f"unparseable expires_at id={tid}")
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    days = (expires - now).total_seconds() / 86400
    detail = f"created={created} expires={exp_str} id={tid} days_left={days:.0f}"
    if days <= 0:
        results.add("ci-token", CI_TOKEN_NAME, FAIL, "EXPIRED " + detail)
    elif days <= CI_TOKEN_WARN_DAYS:
        results.add("ci-token", CI_TOKEN_NAME, WARN, "expiring soon " + detail)
    else:
        results.add("ci-token", CI_TOKEN_NAME, PASS, detail)


# --- Check group 5: AI Gateway Anthropic route health ------------------------
def check_ai_health(results, session):
    body = {"model": "claude-sonnet-4-5-20250929", "max_tokens": 16,
            "messages": [{"role": "user", "content": "ping"}]}
    status, _, neterr = coder_request(
        "POST", "/api/v2/aibridge/anthropic/v1/messages",
        body=body,
        extra_headers={"x-api-key": session,
                       "anthropic-version": "2023-06-01"})
    if neterr is not None:
        results.add("ai-health", "anthropic-route", WARN,
                    f"network error: {neterr}")
    elif status == 200:
        results.add("ai-health", "anthropic-route", PASS, "HTTP 200")
    else:
        results.add("ai-health", "anthropic-route", FAIL, f"HTTP {status}")


# --- Output ------------------------------------------------------------------
def print_table(results):
    headers = ("GROUP", "CHECK", "STATUS", "DETAIL")
    rows = [(r["group"], r["check"], r["status"], r["detail"])
            for r in results.rows]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    # The DETAIL column is free-width (last), so do not pad it.
    fmt = "  ".join("{:<%d}" % w for w in widths[:3]) + "  {}"
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in widths[:3]) + "  " + "-" * widths[3])
    for row in rows:
        print(fmt.format(*row))
    c = results.counts()
    print()
    print(f"PASS={c[PASS]} WARN={c[WARN]} FAIL={c[FAIL]}")


def main():
    ap = argparse.ArgumentParser(description="Read-only drift diagnostic.")
    ap.add_argument("--json", action="store_true",
                    help="emit results as a JSON array instead of a table")
    args = ap.parse_args()

    results = Results()

    # Groups 1 and 2 do not need the Coder API.
    fetched = check_external_secrets(results)
    check_placeholders(results, fetched)

    creds = read_secret("CODER_ADMIN_EMAIL", "CODER_ADMIN_PASSWORD")
    session = None
    if "CODER_ADMIN_EMAIL" in creds and "CODER_ADMIN_PASSWORD" in creds:
        session = coder_login(creds["CODER_ADMIN_EMAIL"],
                              creds["CODER_ADMIN_PASSWORD"])

    if session:
        check_token_lifetime(results, session)
        check_ci_token(results, session)
        check_ai_health(results, session)
    else:
        for group, check in (("token-life", "config"),
                             ("ci-token", CI_TOKEN_NAME),
                             ("ai-health", "anthropic-route")):
            results.add(group, check, WARN, "Coder login unavailable")

    if args.json:
        print(json.dumps(results.rows, indent=2))
    else:
        print_table(results)

    return 1 if results.counts()[FAIL] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
