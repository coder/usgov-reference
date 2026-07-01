#!/usr/bin/env bash
# scripts/check-identifiers.sh
#
# Generic identifier pattern scanner for the usgov-reference public
# repository. Detects classes of real AWS environment identifiers by shape
# using extended regular expressions. The script carries no literal live-env
# values; those live privately in the upstream live-env repo
# (the private upstream repository) and are checked in the promotion pipeline before
# any file is published here.
#
# Pattern classes detected:
#   (1) 12-digit AWS account IDs in ARN or ECR hostname contexts.
#   (2) AWS VPC and networking resource IDs
#       (vpc|subnet|sg|nat|igw|eni|eipalloc|rtb|acl|ami followed by hex).
#   (3) Bare UUIDs (8-4-4-4-12 lowercase hex), covering ACM cert UUIDs,
#       Coder org UUIDs, and any other UUID-shaped identifier.
#   (4) Optional base domain (set FORBIDDEN_BASE_DOMAIN env var). Matching
#       is backslash-aware: detects the domain even when dots are escaped as
#       \. or \\. in regexp config values or YAML double-encoded strings.
#
# Usage:
#   scripts/check-identifiers.sh [REPO_ROOT]
#
# Environment variables:
#   FORBIDDEN_BASE_DOMAIN (optional)
#     When set, occurrences of this domain string trigger a failure.
#     Dots are matched with optional preceding backslashes, so the pattern
#     catches "example.com", "example\.com", and "example\\.com" alike.
#     Example: FORBIDDEN_BASE_DOMAIN=myorg.example.gov bash scripts/check-identifiers.sh
#
# Exit codes:
#   0  - no patterns detected (clean)
#   1  - one or more pattern matches found (CI failure)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${1:-"$(cd "$SCRIPT_DIR/.." && pwd)"}"

echo "Scanning: $REPO_ROOT"
[ -n "${FORBIDDEN_BASE_DOMAIN:-}" ] && echo "Base-domain filter: $FORBIDDEN_BASE_DOMAIN"
echo ""

found=0

# grep_pattern PATTERN DESCRIPTION
# Searches the repository tree with extended-regex PATTERN.
# Skips .git/ internals and binary files (-I).
# Sets found=1 when any match is found.
grep_pattern() {
  local pattern="$1"
  local description="$2"

  local matches
  matches=$(grep -rEI \
    --exclude-dir=".git" \
    -- "$pattern" "$REPO_ROOT" 2>/dev/null || true)

  if [ -n "$matches" ]; then
    printf 'FAIL [%s]:\n' "$description"
    printf '%s\n' "$matches" | sed 's/^/  /'
    echo ""
    found=1
  fi
}

# -----------------------------------------------------------------------
# Pattern 1: 12-digit AWS account IDs in ARN or ECR hostname contexts.
# Only matches when the number appears in a known AWS structural position:
#   arn:<partition>:<service>:<region>:<ACCOUNT>:<resource>
#   <ACCOUNT>.dkr.ecr.<region>.amazonaws.com
# Standalone 12-digit numbers (version strings, timestamps) are ignored.
# -----------------------------------------------------------------------
grep_pattern \
  'arn:[A-Za-z0-9-]+:[A-Za-z0-9-]+:[a-z0-9-]*:[0-9]{12}:' \
  "AWS account ID in ARN"

grep_pattern \
  '[0-9]{12}\.dkr\.ecr\.' \
  "AWS account ID in ECR hostname"

# -----------------------------------------------------------------------
# Pattern 2: AWS VPC and networking resource IDs.
# Format: <type>-<8 to 17 lowercase hex digits>
# Matches real IDs such as vpc-0XXXXXXXXXXXXXXX (17 lowercase hex chars).
# Placeholder tokens (<VPC_ID>, <SUBNET_ID>, etc.) contain angle brackets
# and uppercase letters, so they do not match.
# -----------------------------------------------------------------------
grep_pattern \
  '(vpc|subnet|sg|nat|igw|eni|eipalloc|rtb|acl|ami)-[0-9a-f]{8,17}' \
  "AWS resource ID"

# -----------------------------------------------------------------------
# Pattern 3: Bare UUID (8-4-4-4-12 lowercase hex).
# Catches ACM certificate UUIDs, Coder org UUIDs, and any other real
# UUID-shaped identifier. Placeholder tokens like <ACM_CERT_UUID> contain
# angle brackets and uppercase letters so they do not match.
# Word-boundary anchors (\b) prevent partial matches inside longer tokens.
# -----------------------------------------------------------------------
grep_pattern \
  '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' \
  "UUID (ACM cert, Coder org, or similar)"

# -----------------------------------------------------------------------
# Pattern 4: Optional base domain (backslash-aware).
# Set FORBIDDEN_BASE_DOMAIN to detect the domain even when dots appear
# escaped in regexp config values or YAML-encoded strings:
#   plain text:         example.com
#   Go regexp value:    example\.com
#   YAML double-encode: example\\.com
# Each dot in the domain is replaced with the ERE quantifier \\*\. which
# matches zero or more literal backslashes followed by a literal dot.
# -----------------------------------------------------------------------
if [ -n "${FORBIDDEN_BASE_DOMAIN:-}" ]; then
  # sed 's/\./\\\\*\\./g': each '.' -> '\\*\.' in the ERE pattern.
  # ERE \\*\. = zero-or-more backslashes then literal dot.
  pattern="$(printf '%s' "$FORBIDDEN_BASE_DOMAIN" | sed 's/\./\\\\*\\./g')"
  grep_pattern "$pattern" "Forbidden base domain ($FORBIDDEN_BASE_DOMAIN)"
fi

# -----------------------------------------------------------------------
# Result
# -----------------------------------------------------------------------
if [ "$found" -eq 0 ]; then
  echo "OK: no forbidden identifier patterns found. Repository is clean."
  exit 0
else
  echo "FAIL: one or more identifier patterns detected."
  echo "Replace all real values with <PLACEHOLDER> tokens before pushing."
  echo "See CONTRIBUTING.md for the promotion model and placeholder style."
  exit 1
fi
