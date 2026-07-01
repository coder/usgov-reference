#!/usr/bin/env bash
# preflight-readiness.sh: verify <CLUSTER_NAME> workspace prerequisites (human pre-orchestrator).
# Usage: preflight-readiness.sh [--clone] [--env-file PATH] [--workspace-root PATH]
set -euo pipefail

CLONE=0
ENV_FILE="${HOME}/.config/<CLUSTER_NAME>/env"
WORKSPACE_ROOT=""

usage() {
  cat <<'EOF'
Usage: preflight-readiness.sh [--clone] [--env-file PATH] [--workspace-root DIR]

Verify <CLUSTER_NAME> workspace prerequisites before launching the orchestrator.

Options:
  --clone              git clone missing reference repos into REFERENCE_ROOT
  --env-file PATH      env file (default: ~/.config/<CLUSTER_NAME>/env)
  --workspace-root DIR parent of <CLUSTER_NAME> + reference (auto-detect if omitted)
  -h, --help           show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --clone) CLONE=1; shift ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --workspace-root) WORKSPACE_ROOT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

pass=0
warn=0
fail=0

ok()   { echo "  OK   $*"; pass=$((pass + 1)); }
note() { echo "  NOTE $*"; warn=$((warn + 1)); }
bad()  { echo "  FAIL $*" >&2; fail=$((fail + 1)); }

# Reference clones: directory name => git URL (clone into that directory name)
declare -a REF_DIRS=(
  coder-eks-deployment
  demo-aigov-rhsummit-2026
  homelab
  openshift-servicemesh-inventory-demo
)
declare -a REF_URLS=(
  https://github.com/ausbru87/coder-eks-deployment.git
  https://github.com/coder/demo-aigov-rhaiis-rhsummit-2026.git
  https://github.com/ausbru87/homelab.git
  https://github.com/ausbru87/openshift-servicemesh-inventory-demo.git
)

detect_workspace_root() {
  if [[ -n "$WORKSPACE_ROOT" ]]; then
    echo "$WORKSPACE_ROOT"
    return
  fi
  if [[ -n "${DEMOENV_WORKSPACE_ROOT:-}" ]]; then
    echo "${DEMOENV_WORKSPACE_ROOT/#\~/$HOME}"
    return
  fi
  local dir
  dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  if [[ -d "$dir/reference" ]] || [[ -d "$dir/<CLUSTER_NAME>" ]]; then
    echo "$dir"
    return
  fi
  if [[ -d "$(dirname "$dir")/reference" ]] || [[ -d "$(dirname "$dir")/<CLUSTER_NAME>" ]]; then
    echo "$(dirname "$dir")"
    return
  fi
  echo "$PWD"
}

WORKSPACE_ROOT="$(detect_workspace_root)"
WORKSPACE_ROOT="${WORKSPACE_ROOT/#\~/$HOME}"
expand_home() { echo "${1/#\~/$HOME}"; }

echo "== <CLUSTER_NAME> preflight readiness =="
echo "workspace: $WORKSPACE_ROOT"
echo

# --- env file ---
if [[ -f "$ENV_FILE" ]]; then
  ok "env file: $ENV_FILE"
  # shellcheck disable=SC1090
  set +u
  source "$ENV_FILE"
  set -u
  REFERENCE_ROOT="$(expand_home "${REFERENCE_ROOT:-reference}")"
  DEMOENV_ROOT="$(expand_home "${DEMOENV_ROOT:-<CLUSTER_NAME>}")"
else
  bad "env file missing: $ENV_FILE (see docs/PRE-REQUISITES.md)"
  REFERENCE_ROOT="$WORKSPACE_ROOT/reference"
  DEMOENV_ROOT="$WORKSPACE_ROOT/<CLUSTER_NAME>"
fi

if [[ "$REFERENCE_ROOT" != /* ]]; then
  if ! REFERENCE_ROOT="$(cd "$WORKSPACE_ROOT" && cd "$REFERENCE_ROOT" 2>/dev/null && pwd)"; then
    REFERENCE_ROOT="$WORKSPACE_ROOT/reference"
  fi
fi

DEMOENV_ROOT="${DEMOENV_ROOT:-<CLUSTER_NAME>}"
if [[ "$DEMOENV_ROOT" != /* ]]; then
  if ! DEMOENV_ROOT="$(cd "$WORKSPACE_ROOT" && cd "$DEMOENV_ROOT" 2>/dev/null && pwd)"; then
    DEMOENV_ROOT="$WORKSPACE_ROOT/<CLUSTER_NAME>"
  fi
fi

echo
echo "-- paths --"
if [[ -d "$DEMOENV_ROOT" ]]; then
  ok "target repo: $DEMOENV_ROOT"
else
  note "target repo not found: $DEMOENV_ROOT (git clone git@github.com:coder/<CLUSTER_NAME>.git)"
fi

mkdir -p "$REFERENCE_ROOT" 2>/dev/null || true
if [[ -d "$REFERENCE_ROOT" ]]; then
  ok "reference root: $REFERENCE_ROOT"
else
  bad "cannot create reference root: $REFERENCE_ROOT"
fi

echo
echo "-- reference clones --"
for i in "${!REF_DIRS[@]}"; do
  dir="${REF_DIRS[$i]}"
  url="${REF_URLS[$i]}"
  dest="$REFERENCE_ROOT/$dir"
  if [[ -d "$dest/.git" ]]; then
    sha="$(git -C "$dest" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    ok "$dir @ $sha"
  elif [[ -d "$dest" ]]; then
    note "$dir exists but is not a git repo; remove or re-clone"
  elif [[ "$CLONE" -eq 1 ]]; then
    echo "  CLONE $url -> $dest"
    if git clone --depth 1 "$url" "$dest"; then
      sha="$(git -C "$dest" rev-parse --short HEAD)"
      ok "cloned $dir @ $sha"
    else
      bad "clone failed: $url"
    fi
  else
    bad "missing: $dest (run with --clone or see docs/PRE-REQUISITES.md)"
  fi
done

echo
echo "-- required env vars --"
if [[ -n "${AWS_PROFILE:-}${AWS_ACCESS_KEY_ID:-}" ]]; then
  ok "AWS GovCloud credentials configured (profile or keys)"
else
  bad "AWS GovCloud credentials not set (AWS_PROFILE or keys)"
fi

if [[ -n "${AWS_COMMERCIAL_PROFILE:-}" ]]; then
  ok "AWS_COMMERCIAL_PROFILE=$AWS_COMMERCIAL_PROFILE"
else
  bad "AWS_COMMERCIAL_PROFILE unset (needed for NS delegation WS-01)"
fi

if [[ -n "${CODER_LICENSE:-}" ]]; then
  ok "CODER_LICENSE set (${#CODER_LICENSE} chars)"
else
  bad "CODER_LICENSE unset (needed for WS-07)"
fi

echo
echo "-- AWS identity --"
if command -v aws >/dev/null 2>&1; then
  ok "aws CLI: $(aws --version 2>&1 | head -1)"
  region="${AWS_DEFAULT_REGION:-us-gov-west-1}"
  if aws sts get-caller-identity --region "$region" >/dev/null 2>&1; then
    id="$(aws sts get-caller-identity --region "$region" --query Account --output text 2>/dev/null)"
    ok "GovCloud sts: account $id region $region"
  else
    bad "aws sts get-caller-identity --region $region failed"
  fi
  if [[ -n "${AWS_COMMERCIAL_PROFILE:-}" ]]; then
    if aws sts get-caller-identity --profile "$AWS_COMMERCIAL_PROFILE" >/dev/null 2>&1; then
      cid="$(aws sts get-caller-identity --profile "$AWS_COMMERCIAL_PROFILE" --query Account --output text 2>/dev/null)"
      ok "Commercial sts (profile $AWS_COMMERCIAL_PROFILE): account $cid"
    else
      bad "commercial sts failed for profile $AWS_COMMERCIAL_PROFILE"
    fi
  fi
else
  bad "aws CLI not found"
fi

echo
echo "-- CLI tools --"
require_cmd() {
  local cmd="$1" hint="${2:-}"
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$cmd: $(command -v "$cmd")"
  else
    bad "$cmd not found${hint:+, $hint}"
  fi
}

require_cmd git
require_cmd curl
require_cmd terraform "need >= 1.9"
require_cmd tmux "recommended for overnight orchestrator"
optional_cmd() {
  if command -v "$1" >/dev/null 2>&1; then
    ok "$1 (optional): $(command -v "$1")"
  else
    note "$1 not installed (needed later)"
  fi
}
optional_cmd kubectl
optional_cmd helm
optional_cmd claude
optional_cmd docker

if command -v claude >/dev/null 2>&1; then
  ver="$(claude --version 2>/dev/null | head -1 || true)"
  [[ -n "$ver" ]] && note "claude version: $ver (need >= 2.1.154 for Opus 4.8 / ultracode)"
fi

echo
echo "-- doc pack (optional) --"
if [[ -f "$DEMOENV_ROOT/docs/AGENT-PRD.md" ]]; then
  ok "AGENT-PRD.md present"
elif [[ -f "$DEMOENV_ROOT/docs/swarm/ORCHESTRATOR.md" ]]; then
  ok "swarm docs present"
else
  note "doc pack not in $DEMOENV_ROOT/docs; clone github.com/coder/<CLUSTER_NAME>"
fi

echo
echo "======================================="
echo "PASS: $pass  WARN: $warn  FAIL: $fail"
if [[ "$fail" -gt 0 ]]; then
  echo "Not ready. Fix FAIL items above." >&2
  [[ "$CLONE" -eq 0 ]] && echo "Tip: re-run with --clone to fetch reference repos." >&2
  exit 1
fi
if [[ "$warn" -gt 0 ]]; then
  echo "Ready with warnings."
  exit 0
fi
echo "Ready."
exit 0
