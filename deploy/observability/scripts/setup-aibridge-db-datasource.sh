#!/usr/bin/env sh
# Provision the read-only Coder DB role and the Grafana datasource credential
# for the AI Governance dashboard's usage, cost, and session drill-downs.
#
# What it does (idempotent):
#   1. Reads (or generates) the grafana_ro password and publishes it to the
#      Kubernetes Secret aigov-grafana-db (key AIGOV_DB_PASSWORD) in the
#      monitoring namespace. Grafana reads it as ${AIGOV_DB_PASSWORD} (wired via
#      kube-prometheus-stack-values.yaml grafana.envValueFrom).
#   2. Applies sql/aibridge-grafana-ro.sql as the RDS master user to create the
#      least-privilege role grafana_ro and grant SELECT on the AI Gateway /
#      Agent Firewall tables.
#   3. Applies the datasource ConfigMap so the Grafana sidecar provisions it.
#
# The password is never written to git or echoed. The RDS master credential is
# read from AWS Secrets Manager (<CLUSTER_NAME>/rds/master).
#
# Requirements: kubectl (KUBECONFIG set), aws CLI (profile with read access to
# <CLUSTER_NAME>/rds/master), and a psql reachable to the RDS endpoint. RDS is
# in private subnets, so PSQL must run where the database is reachable. Override
# the psql invocation with PSQL_CMD when running outside the cluster network, for
# example by routing it through an in-cluster pod that ships a psql client.
#
# Usage:
#   . ~/.config/<CLUSTER_NAME>/env
#   export KUBECONFIG=/path/to/kubeconfig
#   sh deploy/observability/scripts/setup-aibridge-db-datasource.sh
set -eu

NS_MONITORING="${NS_MONITORING:-monitoring}"
SECRET_NAME="${SECRET_NAME:-aigov-grafana-db}"
RDS_MASTER_SECRET="${RDS_MASTER_SECRET:-<CLUSTER_NAME>/rds/master}"
PSQL_CMD="${PSQL_CMD:-psql}"
SCRIPT_DIR="$(CDPATH= cd "$(dirname "$0")" && pwd)"
OBS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

umask 077
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT INT TERM

echo "==> Resolving grafana_ro password"
if kubectl -n "$NS_MONITORING" get secret "$SECRET_NAME" >/dev/null 2>&1; then
  echo "    Reusing existing Secret $SECRET_NAME"
  kubectl -n "$NS_MONITORING" get secret "$SECRET_NAME" \
    -o jsonpath='{.data.AIGOV_DB_PASSWORD}' | base64 -d > "$WORK/pw"
else
  echo "    Generating a new password"
  # 32 hex chars, no shell-special or URL-special characters.
  openssl rand -hex 16 > "$WORK/pw"
fi
PW="$(cat "$WORK/pw")"

echo "==> Reading RDS master credential from $RDS_MASTER_SECRET"
aws secretsmanager get-secret-value --secret-id "$RDS_MASTER_SECRET" \
  --query SecretString --output text > "$WORK/master.json"
MUSER="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["username"])' "$WORK/master.json")"
MHOST="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["host"])' "$WORK/master.json")"
MPORT="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["port"])' "$WORK/master.json")"
MURL="$(python3 -c 'import json,sys,urllib.parse as u;d=json.load(open(sys.argv[1]));print("postgres://%s:%s@%s:%s/coder?sslmode=require"%(d["username"],u.quote(str(d["password"]),safe=""),d["host"],d["port"]))' "$WORK/master.json")"

echo "==> Applying role + grants as $MUSER (host $MHOST:$MPORT, db coder)"
# shellcheck disable=SC2086
$PSQL_CMD "$MURL" -v ON_ERROR_STOP=1 -v pw="$PW" -f "$OBS_DIR/sql/aibridge-grafana-ro.sql"

echo "==> Publishing password to Secret $NS_MONITORING/$SECRET_NAME"
kubectl -n "$NS_MONITORING" create secret generic "$SECRET_NAME" \
  --from-literal=AIGOV_DB_PASSWORD="$PW" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS_MONITORING" label secret "$SECRET_NAME" \
  app.kubernetes.io/part-of=<CLUSTER_NAME> \
  app.kubernetes.io/component=grafana-datasource-credential --overwrite

echo "==> Provisioning the Grafana datasource ConfigMap"
kubectl apply -f "$OBS_DIR/datasource-aibridge-postgres.yaml"

cat <<EOF

Done. grafana_ro is read-only and the datasource credential is in
$NS_MONITORING/$SECRET_NAME. Ensure Grafana has the AIGOV_DB_PASSWORD env
(kube-prometheus-stack-values.yaml grafana.envValueFrom); if it was just added,
restart Grafana once:

  kubectl -n $NS_MONITORING rollout restart deploy/kps-grafana

Verify the datasource health in Grafana:
  /api/datasources/uid/aibridge-postgres/health
EOF
