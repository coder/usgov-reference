#!/usr/bin/env bash
# mirror-images.sh - copy upstream images into private ECR.
#
# ECR pull-through cache is not supported in AWS GovCloud (US), so this script
# replaces it: it copies images from Docker Hub / GHCR / Quay into private ECR
# repositories using crane. Workspaces/clusters then pull from ECR via IRSA.
#
# Usage:
#   source ~/.config/<CLUSTER_NAME>/env
#   scripts/mirror-images.sh [--file scripts/images.txt] [--dry-run]
#
# Auth:
#   Docker Hub  - DOCKERHUB_USERNAME + DOCKERHUB_TOKEN (avoids anon rate limit).
#   GHCR / Quay - anonymous (images used here are public).
#   ECR         - aws ecr get-login-password (uses AWS_PROFILE/region from env).
set -euo pipefail

IMAGE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/images.txt"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: mirror-images.sh [--file PATH] [--dry-run]

Copy upstream images listed in the image file into private ECR.

Options:
  --file PATH   image list (default: scripts/images.txt)
  --dry-run     print actions without logging in, creating repos, or copying
  -h, --help    show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --file) IMAGE_FILE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

err() { echo "ERROR: $*" >&2; exit 1; }

command -v crane >/dev/null 2>&1 || err "crane not found in PATH"
command -v aws   >/dev/null 2>&1 || err "aws not found in PATH"

REGION="${AWS_DEFAULT_REGION:-us-gov-west-1}"
[[ -f "$IMAGE_FILE" ]] || err "image file not found: $IMAGE_FILE"

# Resolve ECR registry host from the caller's account.
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)" \
  || err "unable to resolve AWS account (check AWS_PROFILE / credentials)"
ECR_REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
echo "ECR registry: ${ECR_REGISTRY}"
echo "Image file:   ${IMAGE_FILE}"
[[ "$DRY_RUN" == "1" ]] && echo "(dry run)"

# --- Registry logins ------------------------------------------------------
if [[ "$DRY_RUN" != "1" ]]; then
  if [[ -n "${DOCKERHUB_TOKEN:-}" && -n "${DOCKERHUB_USERNAME:-}" ]]; then
    echo "Logging in to Docker Hub as ${DOCKERHUB_USERNAME}"
    printf '%s' "$DOCKERHUB_TOKEN" | crane auth login index.docker.io \
      -u "$DOCKERHUB_USERNAME" --password-stdin
  else
    echo "WARN: DOCKERHUB_USERNAME/TOKEN not set; Docker Hub pulls are anonymous (rate-limited)" >&2
  fi

  echo "Logging in to ECR ${ECR_REGISTRY}"
  aws ecr get-login-password --region "$REGION" \
    | crane auth login "$ECR_REGISTRY" -u AWS --password-stdin
fi

# Map an upstream ref to an ECR repository path (without tag/host).
#   docker.io/library/nginx        -> docker-hub/library/nginx
#   ghcr.io/org/app                -> ghcr/org/app
#   quay.io/keycloak/keycloak      -> quay/keycloak/keycloak
ecr_repo_path() {
  local ref="$1" host rest
  host="${ref%%/*}"
  rest="${ref#*/}"
  case "$host" in
    docker.io|registry-1.docker.io|index.docker.io)
      # Bare names like "nginx" imply library/.
      [[ "$rest" == */* ]] || rest="library/${rest}"
      echo "docker-hub/${rest}" ;;
    ghcr.io) echo "ghcr/${rest}" ;;
    quay.io) echo "quay/${rest}" ;;
    gcr.io) echo "gcr/${rest}" ;;
    *.dkr.ecr.*.amazonaws.com) err "refusing to mirror an ECR ref: $ref" ;;
    *) err "unsupported upstream registry host: $host (ref: $ref)" ;;
  esac
}

ensure_repo() {
  local repo="$1"
  if aws ecr describe-repositories --region "$REGION" \
       --repository-names "$repo" >/dev/null 2>&1; then
    return 0
  fi
  echo "  creating ECR repo: $repo"
  [[ "$DRY_RUN" == "1" ]] && return 0
  aws ecr create-repository --region "$REGION" \
    --repository-name "$repo" \
    --image-tag-mutability IMMUTABLE \
    --image-scanning-configuration scanOnPush=true >/dev/null
}

fail=0 count=0
while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%%#*}"               # strip comments
  line="$(echo "$line" | xargs)"   # trim whitespace
  [[ -z "$line" ]] && continue

  src="$line"
  # Default missing host to docker.io.
  case "$src" in
    */*.*/*|*.*/*) : ;;            # already has a registry host
    *) src="docker.io/${src}" ;;
  esac

  ref_no_tag="${src%@*}"; ref_no_tag="${ref_no_tag%:*}"
  tag="${src##*:}"; [[ "$tag" == "$src" ]] && tag="latest"

  repo_path="$(ecr_repo_path "$ref_no_tag")"
  dst="${ECR_REGISTRY}/${repo_path}:${tag}"

  echo "-> ${src}"
  echo "   ${dst}"
  ensure_repo "$repo_path" || { echo "   FAIL ensure repo" >&2; fail=$((fail+1)); continue; }

  if [[ "$DRY_RUN" == "1" ]]; then
    count=$((count+1)); continue
  fi

  if crane copy "$src" "$dst"; then
    count=$((count+1))
  else
    echo "   FAIL copy" >&2; fail=$((fail+1))
  fi
done < "$IMAGE_FILE"

echo "Mirrored ${count} image(s); ${fail} failure(s)."
[[ "$fail" -eq 0 ]]
