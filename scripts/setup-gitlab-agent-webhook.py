#!/usr/bin/env python3
"""
setup-gitlab-agent-webhook.py - configure the GitLab project webhook that drives
the WS-23 GitLab to Coder bridge, and (optionally) simulate the attributed
spawn so the demo can run before the bridge is deployed.

DESIGN
  A GitLab Issue-events webhook on <GITLAB_GROUP>/coder-templates delivers
  issue events to an in-cluster webhook bridge. The bridge verifies the shared
  X-Gitlab-Token and, for an issue that has a
  coder-* label AND an assignee, attributes the work to the assignee:
    - coder-workspace[:template] -> POST /api/v2/users/<assignee>/workspaces
    - coder-agent[:template]     -> POST /api/v2/tasks/<assignee>   (AI task)
    - coder-task[:template]      -> alias of coder-agent
  The owner is always the assignee, never the author. Agent wins when both
  labels are present, mirroring the Red Hat Summit bridge.

WHAT IT DOES (plan first, mutate only with --apply)
  Webhook mode (default):
    - resolve project <GITLAB_GROUP>/coder-templates (PROJECT_ID) and its existing hooks
    - idempotently create or update an Issue-events webhook pointing at the
      bridge URL, with token verification, all other event types disabled
  Simulate mode (--simulate --issue N): no webhook needed; reproduces exactly
  what the bridge would do for issue N:
    - read the issue (assignee, labels, title, body) via the GitLab API
    - pick the mode from the labels and resolve the template active version
    - print the exact attributed call; with --apply, send it

CREDENTIALS (never logged; lengths only)
  GitLab admin PAT: $GITLAB_ADMIN_PAT, else ASM <CLUSTER_NAME>/gitlab/admin-pat.
  Webhook shared secret: $BRIDGE_WEBHOOK_SECRET, else the `webhook-secret` key of
    ASM <CLUSTER_NAME>/agent-attribution/bridge.
  Coder token (simulate only): $CODER_TASK_BOT_TOKEN, else the `coder-token` key
    of ASM <CLUSTER_NAME>/agent-attribution/bridge, else admin login from
    ~/.config/<CLUSTER_NAME>/generated-secrets.env.

SAFETY
  --plan (default) only performs read-only GETs and prints intended actions.
  --apply performs the webhook create/update (and, in simulate, the spawn).
  This script never requires the bridge to be running; --check-bridge probes it
  but a failed probe is a warning, not an error.

Usage:
    python3 scripts/setup-gitlab-agent-webhook.py                       # plan
    python3 scripts/setup-gitlab-agent-webhook.py --apply                # register
    python3 scripts/setup-gitlab-agent-webhook.py --simulate --issue 7   # print
    python3 scripts/setup-gitlab-agent-webhook.py --simulate --issue 7 --apply
Options:
    --apply              perform mutations (register hook, or send the spawn)
    --plan               read-only plan (default)
    --url URL            bridge webhook URL (default in-cluster Service URL)
    --simulate           simulate the bridge for a single issue (--issue N)
    --issue N            issue IID for --simulate
    --check-bridge       probe the bridge /readyz (warning only)
    --verbose            print extra detail
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

GITLAB_API = os.environ.get(
    "GITLAB_API_URL", "https://gitlab.<BASE_DOMAIN>/api/v4").rstrip("/")
CODER_URL = os.environ.get("DEMO_CODER_URL", "https://dev.<BASE_DOMAIN>").rstrip("/")
SECRETS_ENV = os.path.expanduser("~/.config/<CLUSTER_NAME>/generated-secrets.env")
REGION = "us-gov-west-1"
ASM_BRIDGE_NAME = "<CLUSTER_NAME>/agent-attribution/bridge"
ASM_GITLAB_PAT_NAME = "<CLUSTER_NAME>/gitlab/admin-pat"

PROJECT_ID = 2  # update for your deployment; retrieve: GET /api/v4/projects/<GITLAB_GROUP>%2Fcoder-templates
PROJECT_PATH = "<GITLAB_GROUP>/coder-templates"
DEFAULT_BRIDGE_URL = (
    "http://agent-attribution-bridge.coder.svc.cluster.local:8080/webhook")
DEFAULT_TEMPLATE = "claude-code"
WORKSPACE_LABEL = "coder-workspace"
AGENT_LABELS = ("coder-agent", "coder-task")  # coder-task aliases coder-agent
GIT_REPO_PARAM = "git_repo"
CODER_ORG_ID = "<CODER_ORG_UUID_DEFAULT>"  # org "coder"; retrieve: coder org list --output json | jq -r '.[] | select(.name=="coder") | .id'


# --- shared helpers --------------------------------------------------------

def read_secrets():
    out = {}
    try:
        with open(SECRETS_ENV) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    out[k] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


SECRETS = read_secrets()


def mask_len(s):
    return f"{len(s)} chars" if s else "(empty)"


def http(method, url, headers, body=None):
    data = None
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            headers = {**headers, "Content-Type": "application/json"}
        else:
            data = body
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req)
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


# --- AWS Secrets Manager ---------------------------------------------------

def asm_get_string(name):
    import subprocess
    r = subprocess.run(
        ["aws", "secretsmanager", "get-secret-value", "--region", REGION,
         "--secret-id", name, "--query", "SecretString", "--output", "text"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        return None
    return r.stdout.decode().strip() or None


def asm_json_key(name, key):
    raw = asm_get_string(name)
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj.get(key)
    except ValueError:
        return None
    return None


# --- credential resolution -------------------------------------------------

def gitlab_pat():
    env = os.environ.get("GITLAB_ADMIN_PAT")
    if env:
        return env, "env GITLAB_ADMIN_PAT"
    raw = asm_get_string(ASM_GITLAB_PAT_NAME)
    if raw:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict) and obj.get("token"):
                return obj["token"], f"ASM {ASM_GITLAB_PAT_NAME}"
        except ValueError:
            return raw, f"ASM {ASM_GITLAB_PAT_NAME}"
    return None, "none"


def webhook_secret():
    env = os.environ.get("BRIDGE_WEBHOOK_SECRET")
    if env:
        return env, "env BRIDGE_WEBHOOK_SECRET"
    val = asm_json_key(ASM_BRIDGE_NAME, "webhook-secret")
    if val:
        return val, f"ASM {ASM_BRIDGE_NAME}[webhook-secret]"
    return None, "none"


def gl(method, path, pat, body=None):
    return http(method, GITLAB_API + path,
                {"PRIVATE-TOKEN": pat, "Accept": "application/json"}, body)


# --- Coder (simulate) ------------------------------------------------------

def coder_token():
    env = os.environ.get("CODER_TASK_BOT_TOKEN")
    if env:
        return env, "env CODER_TASK_BOT_TOKEN"
    val = asm_json_key(ASM_BRIDGE_NAME, "coder-token")
    if val:
        return val, f"ASM {ASM_BRIDGE_NAME}[coder-token]"
    email = SECRETS.get("CODER_ADMIN_EMAIL")
    pw = SECRETS.get("CODER_ADMIN_PASSWORD")
    if email and pw:
        status, res = http("POST", CODER_URL + "/api/v2/users/login", {},
                           {"email": email, "password": pw})
        if status == 201 and isinstance(res, dict):
            return res["session_token"], "admin login (generated-secrets.env)"
    return None, "none"


def coder_api(method, path, token, body=None):
    return http(method, CODER_URL + path, {"Coder-Session-Token": token}, body)


def resolve_template_version(token, name):
    """Return (template_version_id, note) for the named template's active version."""
    status, templates = coder_api("GET", "/api/v2/templates", token)
    if status != 200 or not isinstance(templates, list):
        return None, f"GET /api/v2/templates -> {status}"
    for t in templates:
        if t.get("name") == name:
            return t.get("active_version_id"), f"template {name} active version"
    return None, f"template {name} not found"


def template_declares_git_repo(token, version_id):
    """True when the template version declares the git_repo rich parameter."""
    if not version_id:
        return False
    status, params = coder_api(
        "GET", f"/api/v2/templateversions/{version_id}/rich-parameters", token)
    if status != 200 or not isinstance(params, list):
        return False
    return any(p.get("name") == GIT_REPO_PARAM for p in params)


# --- mode + name + prompt --------------------------------------------------

def extract_mode(labels):
    """Return (mode, slug): 'agent'|'workspace'|None. Agent wins when both
    present. Labels may be strings (REST issue API) or {title} dicts."""
    titles = []
    for raw in labels or []:
        titles.append((raw.get("title") if isinstance(raw, dict) else str(raw)).strip())
    ws = (None, "")
    for title in titles:
        for base in AGENT_LABELS:
            if title == base:
                return "agent", ""
            if title.startswith(base + ":"):
                return "agent", title.split(":", 1)[1].strip().lower()
        if title == WORKSPACE_LABEL:
            ws = ("workspace", "")
        elif title.startswith(WORKSPACE_LABEL + ":"):
            ws = ("workspace", title.split(":", 1)[1].strip().lower())
    return ws


def workspace_name(project_path, iid):
    repo = project_path.rsplit("/", 1)[-1].lower()
    repo = "".join(c if (c.isalnum() or c == "-") else "-" for c in repo).strip("-")
    suffix = f"-issue-{iid}"
    if not repo:
        return suffix.lstrip("-")
    if len(repo) + len(suffix) > 32:
        repo = repo[:32 - len(suffix)].rstrip("-")
    return repo + suffix


def seed_prompt(issue, project_path, iid, repo_web_url):
    title = (issue or {}).get("title", "")
    desc = (issue or {}).get("description", "") or ""
    url = (issue or {}).get("web_url", "")
    if title:
        body = (f"You have been assigned GitLab issue #{iid} in `{project_path}`.\n\n"
                f"Title: {title}\n\n")
        if desc.strip():
            body += f"Description:\n\n{desc}\n\n"
        body += (f"Source: {url}\nRepository: {repo_web_url}\n\n"
                 "Clone the repository above, investigate the request, and make "
                 "the needed changes. When you are done, push a branch and open a "
                 f"Merge Request that references the issue (Closes #{iid}).")
        return body
    return (f"Work on GitLab issue {url} in repository {repo_web_url}. Clone the "
            f"repo, investigate, make the changes, then open a Merge Request that "
            f"closes issue #{iid}.")


# --- webhook registration --------------------------------------------------

def register_webhook(url, apply, verbose):
    pat, pat_source = gitlab_pat()
    secret, secret_source = webhook_secret()
    print(f"Bridge URL           : {url}")
    print(f"GitLab admin PAT     : {pat_source}")
    print(f"Webhook shared secret: {secret_source} ({mask_len(secret or '')})")
    if url.startswith("http://"):
        print("NOTE: an in-cluster http:// target requires GitLab admin setting "
              "'Allow requests to the local network from webhooks', or expose the "
              "bridge via ingress and use an https:// URL.")
    if pat is None:
        print("\nNo GitLab admin PAT available; cannot read or register hooks.")
        print("Provide $GITLAB_ADMIN_PAT or populate ASM "
              f"{ASM_GITLAB_PAT_NAME}, then re-run.")
        return 1

    status, hooks = gl("GET", f"/projects/{PROJECT_ID}/hooks", pat)
    if status != 200 or not isinstance(hooks, list):
        print(f"GET project {PROJECT_ID} hooks -> {status} {hooks}")
        return 1
    existing = next((h for h in hooks if h.get("url") == url), None)

    payload = {
        "url": url,
        "issues_events": True,
        "push_events": False,
        "merge_requests_events": False,
        "tag_push_events": False,
        "note_events": False,
        "enable_ssl_verification": url.startswith("https://"),
    }
    if secret:
        payload["token"] = secret

    if existing:
        action = "UPDATE"
        detail = f"hook id {existing['id']} (issues_events, token refreshed)"
    else:
        action = "CREATE"
        detail = "new Issue-events hook with token verification"
    print(f"\n[{action}] project {PROJECT_PATH} (id {PROJECT_ID}): {detail}")

    if not apply:
        print("\n(read-only plan; no changes made. Re-run with --apply.)")
        if secret is None:
            print("WARNING: no webhook secret resolved; --apply would register a "
                  "hook WITHOUT token verification. Populate the secret first.")
        return 0
    if secret is None:
        print("Refusing to register a hook without a shared secret. "
              "Populate $BRIDGE_WEBHOOK_SECRET or ASM "
              f"{ASM_BRIDGE_NAME}[webhook-secret] first.")
        return 1

    if existing:
        code, res = gl("PUT", f"/projects/{PROJECT_ID}/hooks/{existing['id']}",
                       pat, payload)
    else:
        code, res = gl("POST", f"/projects/{PROJECT_ID}/hooks", pat, payload)
    ok = code in (200, 201)
    print(f"  {'ok' if ok else 'FAIL'}: hook {action.lower()} -> {code}"
          + ("" if ok else f" {res}"))
    return 0 if ok else 1


# --- simulate --------------------------------------------------------------

def simulate(iid, apply, verbose):
    pat, pat_source = gitlab_pat()
    if pat is None:
        print("simulate requires a GitLab admin PAT to read the issue.")
        return 1
    status, issue = gl("GET", f"/projects/{PROJECT_ID}/issues/{iid}", pat)
    if status != 200 or not isinstance(issue, dict):
        print(f"GET issue {iid} -> {status} {issue}")
        return 1

    assignees = issue.get("assignees") or []
    assignee = assignees[0]["username"] if assignees else None
    labels = issue.get("labels") or []
    mode, slug = extract_mode(labels)

    print(f"Issue #{iid}: {issue.get('title', '')!r}")
    print(f"  labels   : {labels}")
    print(f"  assignee : {assignee or '(none)'}")
    print(f"  mode     : {mode or '(none)'}" + (f" (slug {slug})" if slug else ""))
    if mode is None:
        print(f"NO-OP: issue lacks a {WORKSPACE_LABEL}/{AGENT_LABELS[0]} label.")
        return 0
    if not assignee:
        print("NO-OP: issue has no assignee (assign it to attribute the spawn).")
        return 0

    token, token_source = coder_token()
    if token is None:
        print("simulate requires a Coder token to resolve the template version.")
        return 1
    template = slug or DEFAULT_TEMPLATE
    tv_id, tv_note = resolve_template_version(token, template)
    name = workspace_name(PROJECT_PATH, iid)
    project_web = issue.get("web_url", "").rsplit("/-/issues/", 1)[0]

    print(f"\nCoder token  : {token_source}")
    print(f"Template     : {tv_note} -> {tv_id}")

    if mode == "agent":
        prompt = seed_prompt(issue, PROJECT_PATH, iid, project_web)
        print("\nAttributed AI task request (owner = assignee):")
        print(f"  POST {CODER_URL}/api/v2/tasks/{assignee}")
        print("  body: " + json.dumps(
            {"template_version_id": tv_id, "name": name, "input": "<issue prompt>"}))
        if verbose:
            print(f"\n  input prompt:\n{prompt}\n")
        body = {"template_version_id": tv_id, "name": name, "input": prompt}
        path = f"/api/v2/tasks/{assignee}"
    else:
        rich = None
        if template_declares_git_repo(token, tv_id):
            rich = [{"name": GIT_REPO_PARAM, "value": project_web}]
        print("\nAttributed workspace request (owner = assignee):")
        print(f"  POST {CODER_URL}/api/v2/users/{assignee}/workspaces")
        shown = {"template_version_id": tv_id, "name": name}
        if rich:
            shown["rich_parameter_values"] = rich
        print("  body: " + json.dumps(shown))
        body = {"template_version_id": tv_id, "name": name}
        if rich:
            body["rich_parameter_values"] = rich
        path = f"/api/v2/users/{assignee}/workspaces"

    if tv_id is None:
        print("\nCannot send: no template version resolved.")
        return 1
    if not apply:
        print("\n(read-only; nothing created. Re-run with --apply to send.)")
        return 0

    code, res = coder_api("POST", path, token, body)
    ok = code == 201
    if ok:
        print(f"\nok: {mode} created, owner {assignee}, workspace {name} "
              f"(id {res.get('id') if isinstance(res, dict) else '?'})")
    else:
        print(f"\nFAIL: POST {path} -> {code} {res}")
    return 0 if ok else 1


def check_bridge(url):
    base = url.rsplit("/webhook", 1)[0]
    try:
        status, _ = http("GET", base + "/readyz", {})
        print(f"bridge /readyz -> {status}")
    except Exception as e:  # noqa: BLE001 - probe is best-effort
        print(f"bridge probe failed (warning only): {e}")


# --- main ------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Configure the WS-23 GitLab webhook.")
    ap.add_argument("--apply", action="store_true", help="perform mutations")
    ap.add_argument("--plan", action="store_true", help="read-only plan (default)")
    ap.add_argument("--url", default=DEFAULT_BRIDGE_URL, help="bridge webhook URL")
    ap.add_argument("--simulate", action="store_true",
                    help="simulate the bridge for a single issue")
    ap.add_argument("--issue", type=int, help="issue IID for --simulate")
    ap.add_argument("--check-bridge", action="store_true",
                    help="probe the bridge /readyz (warning only)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    if args.apply and args.plan:
        sys.exit("--apply and --plan are mutually exclusive")

    if args.check_bridge:
        check_bridge(args.url)

    if args.simulate:
        if args.issue is None:
            sys.exit("--simulate requires --issue N")
        sys.exit(simulate(args.issue, args.apply, args.verbose))

    sys.exit(register_webhook(args.url, args.apply, args.verbose))


if __name__ == "__main__":
    main()
