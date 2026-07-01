#!/usr/bin/env bash
# govcloud-quota-check.sh - check (and optionally request) AWS Service Quota
# increases needed for the <CLUSTER_NAME> deployment.
#
# Defaults to CHECK-ONLY. Pass --request to actually submit increase requests
# for any quota found below its required value.
#
# Usage:
#   scripts/govcloud-quota-check.sh [--profile NAME] [--region REGION] [--request]
#
# Notes:
# - Service Quotas codes are listable with:
#     aws service-quotas list-service-quotas --service-code <svc>
# - Some quotas are not API-adjustable and require an AWS Support case; the
#   script flags those instead of attempting a request.
set -uo pipefail   # intentionally NOT -e; per-call failures are handled inline

REGION="${AWS_DEFAULT_REGION:-us-gov-west-1}"
PROFILE="${AWS_PROFILE:-}"
REQUEST=0

usage() {
  cat <<'EOF'
Usage: govcloud-quota-check.sh [--profile NAME] [--region REGION] [--request]

  --profile NAME   AWS named profile (default: $AWS_PROFILE)
  --region REGION  region (default: us-gov-west-1)
  --request        submit increase requests for quotas below target
  -h, --help       show help

Default is check-only. Edit the QUOTAS table to tune required values.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --request) REQUEST=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

AWS=(aws)
[[ -n "$PROFILE" ]] && AWS+=(--profile "$PROFILE")
AWS+=(--region "$REGION")

command -v jq >/dev/null 2>&1 || { echo "ERROR: jq not found" >&2; exit 1; }

# Required values are tuned for the lean Coder+AI substrate plus headroom.
# Format: service_code|quota_code|friendly name|required_value
QUOTAS=(
  "ec2|L-1216C47A|Running On-Demand Standard (A,C,D,H,I,M,R,T,Z) vCPUs|64"
  "ec2|L-0263D0A3|EC2-VPC Elastic IPs|5"
  "eks|L-1194D53C|EKS clusters per region|2"
  "vpc|L-F678F1CE|VPCs per region|5"
  "vpc|L-FE5A380F|NAT gateways per AZ|5"
  "vpc|L-A4707A72|Internet gateways per region|5"
  "elasticloadbalancing|L-69A177A2|Network Load Balancers per region|5"
  "rds|L-7B6409FD|RDS DB instances|10"
  "rds|L-7ADDB58A|RDS total storage (GB)|1000"
)

# Identity preflight.
if ! ID=$("${AWS[@]}" sts get-caller-identity --query Arn --output text 2>&1); then
  echo "ERROR: cannot authenticate (profile='${PROFILE:-default}', region='$REGION'):" >&2
  echo "  $ID" >&2
  exit 1
fi
echo "Identity: $ID"
echo "Region:   $REGION"
echo "Mode:     $([[ $REQUEST == 1 ]] && echo 'CHECK + REQUEST' || echo 'CHECK ONLY')"
echo

ok=0 low=0 unknown=0 requested=0

q() { "${AWS[@]}" "$@" 2>/dev/null; }

for entry in "${QUOTAS[@]}"; do
  IFS='|' read -r svc code name req <<<"$entry"

  info=$(q service-quotas get-service-quota --service-code "$svc" --quota-code "$code" --output json)
  [[ -z "$info" ]] && info=$(q service-quotas get-aws-default-service-quota --service-code "$svc" --quota-code "$code" --output json)

  if [[ -z "$info" ]]; then
    printf "  %-7s %-50s (lookup failed: %s/%s)\n" "UNKNOWN" "$name" "$svc" "$code"
    unknown=$((unknown+1)); continue
  fi

  cur=$(echo "$info" | jq -r '.Quota.Value')
  adj=$(echo "$info" | jq -r '.Quota.Adjustable')

  if awk "BEGIN{exit !($cur+0 >= $req+0)}"; then
    printf "  %-7s %-50s current=%s required=%s\n" "OK" "$name" "$cur" "$req"
    ok=$((ok+1)); continue
  fi

  printf "  %-7s %-50s current=%s required=%s adjustable=%s\n" "LOW" "$name" "$cur" "$req" "$adj"
  low=$((low+1))

  [[ $REQUEST == 1 ]] || continue

  if [[ "$adj" != "true" ]]; then
    echo "          -> not API-adjustable; open an AWS Support case"
    continue
  fi

  pending=$(q service-quotas list-requested-service-quota-change-history-by-quota \
    --service-code "$svc" --quota-code "$code" \
    --query 'RequestedQuotas[?Status==`PENDING`||Status==`CASE_OPENED`].[Status,DesiredValue]' \
    --output text)
  if [[ -n "$pending" ]]; then
    echo "          -> request already pending: $pending; skipping"
    continue
  fi

  if out=$("${AWS[@]}" service-quotas request-service-quota-increase \
        --service-code "$svc" --quota-code "$code" --desired-value "$req" --output json 2>&1); then
    rid=$(echo "$out" | jq -r '.RequestedQuota.Id // "?"')
    echo "          -> requested increase to $req (request id: $rid)"
    requested=$((requested+1))
  else
    echo "          -> request FAILED: $out"
  fi
done

echo
echo "Summary: OK=$ok LOW=$low UNKNOWN=$unknown REQUESTED=$requested"
if [[ $low -gt 0 && $REQUEST == 0 ]]; then
  echo "Re-run with --request to submit increases for the LOW quotas."
fi
# Exit non-zero only if something is below target and we did not request it.
[[ $low -eq 0 || $REQUEST == 1 ]]
