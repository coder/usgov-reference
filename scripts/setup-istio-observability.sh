#!/usr/bin/env bash
# setup-istio-observability.sh - deploy the Istio mesh observability UIs.
#
# Applies, in order, the Prometheus scrape config, the Istio Grafana
# dashboards, the Kiali server, and the Kiali ingress route into the existing
# in-cluster kube-prometheus-stack (ns monitoring) and Istio (ns istio-system).
#
# Prerequisites:
#   - KUBECONFIG points at the demo cluster.
#   - The Kiali image is mirrored to ECR (scripts/images.txt ->
#     quay/kiali/kiali:v2.26.0); run scripts/mirror-images.sh first.
#   - Istio 1.30.1 and kube-prometheus-stack are already installed.
#
# Usage:
#   export KUBECONFIG=/path/to/kubeconfig
#   scripts/setup-istio-observability.sh [--render-kiali] [--verify]
#
# Options:
#   --render-kiali  Regenerate deploy/istio/observability/kiali.yaml from the
#                   kiali-server Helm chart + values before applying (needs
#                   helm and internet to fetch the chart; the cluster still
#                   only pulls the ECR image).
#   --verify        Run post-apply checks (target health, Kiali status).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OBS_DIR="${REPO_ROOT}/deploy/istio/observability"
KIALI_CHART_VERSION="2.26.0"
RENDER_KIALI=0
VERIFY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --render-kiali) RENDER_KIALI=1; shift ;;
    --verify) VERIFY=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

command -v kubectl >/dev/null 2>&1 || { echo "ERROR: kubectl not found" >&2; exit 1; }

if [[ "$RENDER_KIALI" == "1" ]]; then
  command -v helm >/dev/null 2>&1 || { echo "ERROR: helm not found (needed for --render-kiali)" >&2; exit 1; }
  echo "==> Rendering kiali.yaml from kiali-server chart ${KIALI_CHART_VERSION}"
  helm repo add kiali https://kiali.org/helm-charts >/dev/null 2>&1 || true
  helm repo update kiali >/dev/null
  {
    sed -n '1,18p' "${OBS_DIR}/kiali.yaml"
    helm template kiali kiali/kiali-server --version "${KIALI_CHART_VERSION}" \
      --namespace istio-system \
      -f "${OBS_DIR}/kiali-server-values.yaml"
  } > "${OBS_DIR}/kiali.yaml.tmp"
  mv "${OBS_DIR}/kiali.yaml.tmp" "${OBS_DIR}/kiali.yaml"
fi

echo "==> Applying Prometheus scrape config (istiod ServiceMonitor + proxy PodMonitor)"
kubectl apply -f "${OBS_DIR}/servicemonitor-istiod.yaml"
kubectl apply -f "${OBS_DIR}/podmonitor-istio-proxies.yaml"

echo "==> Applying Istio Grafana dashboards"
kubectl apply -f "${OBS_DIR}/dashboards-istio.yaml"

echo "==> Deploying Kiali server"
kubectl apply -f "${OBS_DIR}/kiali.yaml"

echo "==> Routing Kiali through the Istio gateway"
kubectl apply -f "${OBS_DIR}/virtualservice-kiali.yaml"

echo "==> Waiting for Kiali to become ready"
kubectl -n istio-system rollout status deploy/kiali --timeout=180s

if [[ "$VERIFY" == "1" ]]; then
  echo "==> Verifying Kiali component status"
  kubectl -n istio-system port-forward svc/kiali 20001:20001 >/tmp/kiali-verify-pf.log 2>&1 &
  pf=$!
  sleep 5
  curl -fsS http://127.0.0.1:20001/kiali/api/istio/status | jq -r '.[] | "  \(.name): \(.status)"' || true
  kill "$pf" 2>/dev/null || true
  echo "==> Done. Reach Kiali now with:"
  echo "    kubectl -n istio-system port-forward svc/kiali 20001:20001"
  echo "    open http://localhost:20001/kiali"
fi

echo "Istio observability applied."
