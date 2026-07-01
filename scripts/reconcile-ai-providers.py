#!/usr/bin/env python3
"""
reconcile-ai-providers.py - reconcile the live Coder AI Gateway providers and
chat model presets against the declarative source of truth in
deploy/coder/ai-providers.yaml.

WHY (v2.34 behavior)
Since Coder v2.34 the AI Gateway providers live in the DATABASE and are managed
through the API (/api/v2/ai/providers); the CODER_AI_GATEWAY_PROVIDER_* Helm env
vars only SEED the DB once on first boot and changing a seeded value later makes
coderd fail to start. This script therefore manages providers via the API and
NEVER via Helm. One shared ai_providers store backs both the AI Gateway
in-workspace path (POST /api/v2/aibridge/<name>/v1/...) and the Coder Agents
model picker (GET /api/experimental/chats/models). Model presets live in a
separate table managed via /api/experimental/chats/model-configs, each tied to
an enabled provider by ai_provider_id.

WHAT IT DOES
Diffs the YAML against the live API and reports/applies create, update, and
disable actions for providers and model presets so the live state matches the
file. Idempotent: a second run with no YAML change is a no-op.

SAFETY
Default mode is a READ-ONLY plan (dry-run). Mutations happen ONLY with --apply.
Provider keys are read from the environment (key_from_env names), passed via the
JSON body over stdin to urllib, NEVER via argv or a URL, and never logged. Key
drift is detected by comparing the server's masked rendering against a locally
computed mask, so the placeholder key is detected and replaced without the real
key ever being printed.

Targets the demo Coder explicitly (NOT the ambient $CODER_URL). Admin creds come
from ~/.config/<CLUSTER_NAME>/generated-secrets.env.

Usage:
    python3 scripts/reconcile-ai-providers.py [--dry-run]   # default: plan only
    python3 scripts/reconcile-ai-providers.py --apply        # operator only
    python3 scripts/reconcile-ai-providers.py --apply --rotate-keys
Options:
    --file PATH        provider source of truth (default: deploy/coder/ai-providers.yaml)
    --dry-run          read-only plan (default when --apply is absent)
    --apply            perform create/update/disable mutations
    --rotate-keys      force-replace provider keys from env even if the mask matches
    --verbose          print extra detail
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required (pip install pyyaml); it is present in the demo env.")

BASE = os.environ.get("DEMO_CODER_URL", "https://dev.<BASE_DOMAIN>").rstrip("/")
SECRETS_ENV = os.path.expanduser("~/.config/<CLUSTER_NAME>/generated-secrets.env")
PROVIDER_ENV = os.path.expanduser("~/.config/<CLUSTER_NAME>/env")
DEFAULT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "deploy", "coder", "ai-providers.yaml",
)

TOKEN = None


# --- helpers ---------------------------------------------------------------

def read_env_file(path):
    """Parse a shell env file into a dict, tolerating an optional `export `."""
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                out[k.strip()] = v
    except FileNotFoundError:
        pass
    return out


def creds():
    c = read_env_file(SECRETS_ENV)
    if "CODER_ADMIN_EMAIL" not in c or "CODER_ADMIN_PASSWORD" not in c:
        sys.exit(f"admin creds not found in {SECRETS_ENV}")
    return c


_PROVIDER_ENV_CACHE = None


def env_value(name):
    """Return an env var value: process environment first, then the provider
    env file. Used for key_from_env lookups. Never logged."""
    global _PROVIDER_ENV_CACHE
    if name in os.environ and os.environ[name] != "":
        return os.environ[name]
    if _PROVIDER_ENV_CACHE is None:
        _PROVIDER_ENV_CACHE = read_env_file(PROVIDER_ENV)
    v = _PROVIDER_ENV_CACHE.get(name)
    return v if v else None


def reveal_length(n):
    if n >= 20:
        return 4
    if n >= 10:
        return 2
    if n >= 5:
        return 1
    return 0


def mask_secret(s):
    """Replicates aibridge/utils.MaskSecret so a locally held key can be
    compared to the server's masked rendering without printing the key."""
    if not s:
        return ""
    runes = list(s)
    reveal = reveal_length(len(runes))
    if len(runes) <= reveal * 2:
        return "..."
    return "".join(runes[:reveal]) + "..." + "".join(runes[-reveal:])


def login():
    body = json.dumps({"email": creds()["CODER_ADMIN_EMAIL"],
                       "password": creds()["CODER_ADMIN_PASSWORD"]}).encode()
    req = urllib.request.Request(BASE + "/api/v2/users/login", data=body,
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))["session_token"]


def api(method, path, body=None):
    headers = {"Coder-Session-Token": TOKEN, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
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


# --- model -----------------------------------------------------------------

class Plan:
    """Collects the intended actions so dry-run can print them and apply can
    execute them in a deterministic order."""

    def __init__(self):
        self.items = []  # (kind, action, name, detail, fn)

    def add(self, kind, action, name, detail, fn=None):
        self.items.append([kind, action, name, detail, fn])

    def counts(self):
        c = {}
        for _, action, _, _, _ in self.items:
            c[action] = c.get(action, 0) + 1
        return c


def load_desired(path):
    with open(path) as f:
        doc = yaml.safe_load(f)
    providers = doc.get("providers") or []
    for p in providers:
        p.setdefault("models", [])
        p.setdefault("enabled", False)
        p.setdefault("base_url", "")
        p.setdefault("display_name", p["name"])
    return providers


def bedrock_settings(p):
    b = p.get("bedrock") or {}
    s = {"_type": "bedrock", "_version": 1}
    if b.get("region"):
        s["region"] = b["region"]
    if b.get("model"):
        s["model"] = b["model"]
    if b.get("small_fast_model"):
        s["small_fast_model"] = b["small_fast_model"]
    return s


def settings_drift(desired, live):
    """Compare the meaningful (non-secret) bedrock settings keys."""
    live = live or {}
    for k in ("region", "model", "small_fast_model"):
        if desired.get(k, "") != (live.get(k) or ""):
            return True
    return False


def key_status(p, live_provider, rotate):
    """Return (state, note) for the provider key. state in
    {noop, set, missing, n/a}. Never returns or logs the key value."""
    if p["type"] == "bedrock" or p.get("auth") == "irsa":
        return ("n/a", "IRSA (no static key)")
    env_name = p.get("key_from_env")
    if not env_name:
        return ("n/a", "no key_from_env declared")
    val = env_value(env_name)
    live_masks = [k.get("masked", "") for k in ((live_provider or {}).get("api_keys") or [])]
    if val is None:
        if live_masks:
            return ("missing", f"${env_name} not set; live has a key {live_masks} (cannot verify)")
        return ("missing", f"${env_name} not set; provider has no key")
    desired_mask = mask_secret(val)
    if not rotate and desired_mask in live_masks:
        return ("noop", f"key matches live ({desired_mask})")
    if live_masks:
        return ("set", f"replace key(s) {live_masks} -> from ${env_name}")
    return ("set", f"set key from ${env_name}")


def provider_by_name(live_list, name):
    for p in live_list:
        if p.get("name") == name:
            return p
    return None


def reconcile_providers(desired, live, plan, rotate):
    live_names = {p.get("name") for p in live}
    desired_names = {p["name"] for p in desired}
    for p in desired:
        name = p["name"]
        live_p = provider_by_name(live, name)
        kstate, knote = key_status(p, live_p, rotate)
        if live_p is None:
            detail = [f"type={p['type']}", f"enabled={p['enabled']}",
                      f"base_url={p['base_url'] or '(empty)'}"]
            if p["type"] == "bedrock":
                detail.append(f"settings={bedrock_settings(p)}")
            else:
                detail.append(f"key={knote}")
            plan.add("provider", "CREATE", name, "; ".join(detail),
                     fn=lambda p=p, kstate=kstate: do_create_provider(p, kstate))
            continue

        # Provider exists: compute field-level drift.
        changes = {}
        if bool(live_p.get("enabled")) != bool(p["enabled"]):
            changes["enabled"] = p["enabled"]
        if (live_p.get("base_url") or "") != (p["base_url"] or ""):
            changes["base_url"] = p["base_url"]
        if (live_p.get("display_name") or "") != (p["display_name"] or ""):
            changes["display_name"] = p["display_name"]
        if p["type"] == "bedrock":
            if settings_drift(bedrock_settings(p), live_p.get("settings")):
                changes["settings"] = bedrock_settings(p)
        set_key = kstate == "set"

        if not changes and not set_key:
            plan.add("provider", "NOOP", name,
                     f"in sync (enabled={live_p.get('enabled')}; key {kstate})")
            continue

        # Label a pure enable->disable transition as DISABLE for clarity.
        action = "UPDATE"
        if changes.get("enabled") is False and live_p.get("enabled"):
            action = "DISABLE"
        notes = [f"{k}={v}" for k, v in changes.items() if k != "settings"]
        if "settings" in changes:
            notes.append(f"settings={changes['settings']}")
        if set_key:
            notes.append(f"key: {knote}")
        plan.add("provider", action, name, "; ".join(notes) or "key change",
                 fn=lambda name=name, changes=dict(changes), p=p, set_key=set_key:
                 do_update_provider(name, changes, p, set_key))

    for p in live:
        if p.get("name") not in desired_names:
            plan.add("provider", "UNMANAGED", p.get("name"),
                     f"present live (enabled={p.get('enabled')}), not in file; left as-is")


def model_matches(mc, provider_id, model):
    same_provider = mc.get("ai_provider_id") == provider_id
    return same_provider and (mc.get("model", "").strip().lower() == model.strip().lower())


# Map the concise YAML cost keys to the API's model_config.cost field names.
_COST_KEYMAP = {
    "input": "input_price_per_million_tokens",
    "output": "output_price_per_million_tokens",
    "cache_read": "cache_read_price_per_million_tokens",
    "cache_write": "cache_write_price_per_million_tokens",
}


def desired_model_config(p, m):
    """Build the model_config object (cost + provider_options) from the YAML
    model entry, or None when neither is declared. Cost is the per-1M-token
    pricing block. reasoning_effort maps to the provider-specific options
    shape: OpenAI uses provider_options.openai.reasoning_effort; Anthropic and
    Bedrock both use provider_options.anthropic.effort."""
    cost = m.get("cost") or {}
    cost_map = {}
    for yk, apik in _COST_KEYMAP.items():
        if cost.get(yk) is not None:
            cost_map[apik] = cost[yk]
    mc = {}
    if cost_map:
        mc["cost"] = cost_map
    effort = m.get("reasoning_effort")
    if effort:
        if p["type"] == "openai":
            mc["provider_options"] = {"openai": {"reasoning_effort": effort}}
        else:
            # Anthropic-direct and Bedrock both use the anthropic options shape.
            mc["provider_options"] = {"anthropic": {"effort": effort}}
    return mc or None


def _as_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def normalize_model_config(mc):
    """Reduce a model_config dict to a comparable (effort, costs) tuple so the
    desired YAML state and the live API state diff cleanly regardless of how
    the server renders decimals (number vs string)."""
    mc = mc or {}
    cost = mc.get("cost") or {}
    costs = tuple(_as_float(cost.get(apik)) for apik in _COST_KEYMAP.values())
    po = mc.get("provider_options") or {}
    effort = None
    if isinstance(po.get("openai"), dict):
        effort = po["openai"].get("reasoning_effort")
    if isinstance(po.get("anthropic"), dict):
        effort = po["anthropic"].get("effort") or effort
    return (effort, costs)


def reconcile_models(desired, live_providers, live_models, plan):
    for p in desired:
        pname = p["name"]
        live_p = provider_by_name(live_providers, pname)
        provider_enabled_desired = bool(p["enabled"])
        for m in p.get("models", []):
            model = m["model"]
            label = f"{pname}/{model}"
            if not provider_enabled_desired:
                plan.add("model", "BLOCKED", label,
                         "provider disabled in file; preset reconciled after the provider is enabled")
                continue
            if live_p is None:
                # Provider will be created first; its id is unknown until then.
                plan.add("model", "CREATE", label,
                         f"after provider {pname} is created; "
                         f"display_name={m.get('display_name', '')!r} "
                         f"enabled={m.get('enabled', True)} default={m.get('default', False)} "
                         f"context_limit={m.get('context_limit')} "
                         f"model_config={normalize_model_config(desired_model_config(p, m))}",
                         fn=lambda p=p, m=m: do_create_model(p, m))
                continue
            existing = next((mc for mc in live_models
                             if model_matches(mc, live_p["id"], model)), None)
            if existing is None:
                plan.add("model", "CREATE", label,
                         f"display_name={m.get('display_name', '')!r} "
                         f"enabled={m.get('enabled', True)} default={m.get('default', False)} "
                         f"context_limit={m.get('context_limit')} "
                         f"model_config={normalize_model_config(desired_model_config(p, m))}",
                         fn=lambda p=p, m=m: do_create_model(p, m))
                continue
            changes = {}
            if bool(existing.get("enabled")) != bool(m.get("enabled", True)):
                changes["enabled"] = m.get("enabled", True)
            if bool(existing.get("is_default")) != bool(m.get("default", False)):
                changes["is_default"] = m.get("default", False)
            if (existing.get("display_name") or "") != (m.get("display_name") or ""):
                changes["display_name"] = m.get("display_name", "")
            if m.get("context_limit") and int(existing.get("context_limit") or 0) != int(m["context_limit"]):
                changes["context_limit"] = int(m["context_limit"])
            desired_mc = desired_model_config(p, m)
            if desired_mc is not None and \
                    normalize_model_config(desired_mc) != normalize_model_config(existing.get("model_config")):
                changes["model_config"] = desired_mc
            if not changes:
                plan.add("model", "NOOP", label, "in sync")
            else:
                detail = "; ".join(
                    f"{k}={normalize_model_config(v) if k == 'model_config' else v}"
                    for k, v in changes.items())
                plan.add("model", "UPDATE", label, detail,
                         fn=lambda mid=existing["id"], changes=dict(changes):
                         do_update_model(mid, changes))


# --- mutations (only invoked under --apply) --------------------------------

def do_create_provider(p, kstate):
    payload = {
        "type": p["type"],
        "name": p["name"],
        "display_name": p["display_name"],
        "enabled": bool(p["enabled"]),
        "base_url": p["base_url"] or "",
    }
    if p["type"] == "bedrock" or p.get("auth") == "irsa":
        payload["settings"] = bedrock_settings(p)
    else:
        val = env_value(p.get("key_from_env"))
        if val is None:
            return (False, f"refusing to create {p['name']} without ${p.get('key_from_env')}")
        payload["api_keys"] = [val]
    code, res = api("POST", "/api/v2/ai/providers", payload)
    ok = code == 201
    return (ok, f"create provider {p['name']} -> {code}" + ("" if ok else f" {res}"))


def do_update_provider(name, changes, p, set_key):
    payload = dict(changes)
    if set_key:
        val = env_value(p.get("key_from_env"))
        if val is None:
            return (False, f"cannot set key: ${p.get('key_from_env')} not set")
        payload["api_keys"] = [{"api_key": val}]
    code, res = api("PATCH", f"/api/v2/ai/providers/{name}", payload)
    ok = code == 200
    redacted = {k: ("<redacted>" if k == "api_keys" else v) for k, v in payload.items()}
    return (ok, f"update provider {name} {list(redacted.keys())} -> {code}" + ("" if ok else f" {res}"))


def resolve_provider_id(name):
    code, res = api("GET", f"/api/v2/ai/providers/{name}")
    if code == 200 and isinstance(res, dict):
        return res.get("id")
    return None


def do_create_model(p, m):
    pid = resolve_provider_id(p["name"])
    if not pid:
        return (False, f"provider {p['name']} not found/enabled; cannot create model {m['model']}")
    payload = {
        "ai_provider_id": pid,
        "model": m["model"],
        "display_name": m.get("display_name", ""),
        "enabled": bool(m.get("enabled", True)),
        "is_default": bool(m.get("default", False)),
        "context_limit": int(m["context_limit"]),
    }
    mc = desired_model_config(p, m)
    if mc is not None:
        payload["model_config"] = mc
    code, res = api("POST", "/api/experimental/chats/model-configs", payload)
    ok = code in (200, 201)
    return (ok, f"create model {p['name']}/{m['model']} -> {code}" + ("" if ok else f" {res}"))


def do_update_model(model_id, changes):
    code, res = api("PATCH", f"/api/experimental/chats/model-configs/{model_id}", changes)
    ok = code == 200
    return (ok, f"update model {model_id} {list(changes.keys())} -> {code}" + ("" if ok else f" {res}"))


# --- driver ----------------------------------------------------------------

def print_plan(plan, verbose):
    print(f"Plan against {BASE} (source: live API, read-only):\n")
    width = max((len(i[2]) for i in plan.items), default=10)
    for kind, action, name, detail, _ in plan.items:
        line = f"  [{action:9}] {kind:8} {name:<{width}}  {detail}"
        if action in ("NOOP", "UNMANAGED") and not verbose:
            line = f"  [{action:9}] {kind:8} {name:<{width}}  {detail}"
        print(line)
    counts = plan.counts()
    print("\nSummary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


def apply_plan(plan):
    print(f"Applying against {BASE}:\n")
    failed = 0
    # Providers first (so model creates find an enabled provider), then models.
    order = {"provider": 0, "model": 1}
    for kind, action, name, detail, fn in sorted(
            plan.items, key=lambda i: order.get(i[0], 9)):
        if action in ("NOOP", "UNMANAGED", "BLOCKED") or fn is None:
            print(f"  [skip {action}] {kind} {name}")
            continue
        ok, msg = fn()
        print(f"  [{'ok' if ok else 'FAIL'}] {msg}")
        if not ok:
            failed += 1
    print(f"\nDone. {failed} failure(s).")
    return failed


def main():
    global TOKEN
    ap = argparse.ArgumentParser(description="Reconcile Coder AI providers + model presets.")
    ap.add_argument("--file", default=DEFAULT_FILE)
    ap.add_argument("--dry-run", action="store_true", help="read-only plan (default)")
    ap.add_argument("--apply", action="store_true", help="perform mutations")
    ap.add_argument("--rotate-keys", action="store_true",
                    help="force-replace provider keys from env even if the mask matches")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    if args.apply and args.dry_run:
        sys.exit("--apply and --dry-run are mutually exclusive")
    apply = args.apply  # default (no flag) is dry-run

    desired = load_desired(args.file)
    TOKEN = login()

    code, live_providers = api("GET", "/api/v2/ai/providers")
    if code != 200:
        sys.exit(f"GET /api/v2/ai/providers failed: {code} {live_providers}")
    code, live_models = api("GET", "/api/experimental/chats/model-configs")
    if code != 200 or not isinstance(live_models, list):
        live_models = []

    plan = Plan()
    reconcile_providers(desired, live_providers, plan, args.rotate_keys)
    reconcile_models(desired, live_providers, live_models, plan)

    if not apply:
        print_plan(plan, args.verbose)
        print("\n(read-only dry-run; no changes made. Re-run with --apply to mutate.)")
        return
    rc = apply_plan(plan)
    sys.exit(1 if rc else 0)


if __name__ == "__main__":
    main()
