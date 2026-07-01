# Observability stack (in-cluster metrics, logs + dashboards)

In-boundary, in-cluster metrics, logs, and dashboards for the GovCloud demo. It
scrapes the Coder control plane's Prometheus metrics, collects pod logs into
Loki, and renders Coder's prebuilt Grafana dashboards with live data, reachable
over HTTPS at `https://grafana.<BASE_DOMAIN>`.

This is the reliable in-cluster implementation. The AWS-native managed variant
(Amazon Managed Prometheus / Grafana, Security Lake) is planned separately and
is not built here.

## What runs

| Piece | Detail |
|---|---|
| Helm release | `kps` = `prometheus-community/kube-prometheus-stack` chart `86.2.0` (prometheus-operator `v0.91.0`), namespace `monitoring`. Values: `kube-prometheus-stack-values.yaml`. |
| Prometheus | StatefulSet `prometheus-kps-kube-prometheus-stack-prometheus`, 20Gi gp3 PVC, 7d retention. Service `kps-kube-prometheus-stack-prometheus:9090`. |
| Grafana | Deployment `kps-grafana`, 5Gi gp3 PVC. Service `kps-grafana:80`. Keycloak SSO (generic OAuth) + local admin break-glass; admin password and OIDC client secret from AWS Secrets Manager via ESO. |
| Prometheus operator | Deployment `kps-kube-prometheus-stack-operator`. Admission webhooks disabled. |
| Coder scrape | `coder-metrics` headless Service (port 2112) + `ServiceMonitor/coder`, both in namespace `coder`. Prometheus job `coder-metrics`. |
| Dashboards | Six Coder dashboards as ConfigMaps in `monitoring`, imported by the Grafana sidecar (label `grafana_dashboard: "1"`). |
| Loki | Single-binary Deployment `loki` (10Gi gp3 PVC, filesystem object store, tsdb schema v13, ~168h retention). Service `loki:3100`. Config `loki.yaml`. Scraped via `ServiceMonitor/loki`. |
| Promtail | DaemonSet `promtail` tailing `/var/log/pods` on every node and pushing to `loki.monitoring.svc:3100`. Config `promtail.yaml`. Scraped via `ServiceMonitor/promtail`. |
| Loki datasource | `loki-datasource.yaml` ConfigMap (label `grafana_datasource: "1"`, uid `loki`), provisioned by the Grafana sidecar. Powers the dashboards' log panels. |
| Ingress | `grafana` Ingress (className `nginx`, host `grafana.<BASE_DOMAIN>`, TLS terminated upstream at the NLB). |

Disabled to keep the demo lean and cut image mirroring: Alertmanager,
node-exporter, kube-state-metrics, bundled alert rules, and the managed EKS
control-plane ServiceMonitors. The kubelet ServiceMonitor is kept so cAdvisor
container CPU and memory metrics power the dashboards' resource panels.

## Images (ECR mirror)

GovCloud has no pull-through cache, so every image is mirrored into private ECR
(`scripts/images.txt` + `scripts/mirror-images.sh`) and the chart values point
at the mirror:

- `quay/prometheus/prometheus:v3.12.0-distroless`
- `quay/prometheus-operator/prometheus-operator:v0.91.0`
- `quay/prometheus-operator/prometheus-config-reloader:v0.91.0`
- `docker-hub/grafana/grafana:13.0.1-security-01`
- `quay/kiwigrid/k8s-sidecar:2.7.3`
- `docker-hub/grafana/loki:3.5.9`
- `docker-hub/grafana/promtail:3.5.9`

## The scrape path

1. `coderd` serves Prometheus metrics on `0.0.0.0:2112` (env vars
   `CODER_PROMETHEUS_ENABLE`, `CODER_PROMETHEUS_ADDRESS`,
   `CODER_PROMETHEUS_COLLECT_AGENT_STATS` in `deploy/coder/values.yaml`). The
   Coder chart's own Service has no metrics port, so `coder-metrics.yaml` adds a
   headless Service that exposes 2112 for the control-plane pod.
2. `ServiceMonitor/coder` selects that Service. Prometheus is configured with
   `serviceMonitorSelectorNilUsesHelmValues: false`, so it discovers the
   ServiceMonitor across namespaces. Scraping adds `namespace` and `pod` target
   labels.
3. The Coder dashboards filter on `namespace="coder"` and `pod=~"coder.*"`,
   which the scraped series satisfy, so panels render without extra config.

## Dashboards

`dashboards-coder.yaml` carries six Prometheus-backed dashboards taken from
`github.com/coder/observability` (`compiled/resources.yaml`): Coder Control
Plane (`coderd`), Coder Status (`coder-status`), Coder Prebuilds, Coder
Provisioners, Coder Workspaces, and Coder Workspace Detail. Every panel targets
datasource uid `prometheus`, which the kube-prometheus-stack Grafana
auto-provisions and marks default.

The purely log-based `agent-boundaries` dashboard is omitted. The log panels in
the workspaces / provisionerd / workspace-detail dashboards target datasource
uid `loki`, which the in-cluster Loki + Promtail stack (below) backs through
`loki-datasource.yaml`, so they resolve and query live log data instead of
erroring.

The AI Governance dashboards (covering the AI Gateway / AI Bridge and the
Agent Firewall / Boundary) are part of a licensed Coder add-on and are not
shipped in this reference. The underlying wiring IS included so those
dashboards can be added under the add-on license: a dedicated read-only
Postgres datasource (`datasource-aibridge-postgres.yaml`, uid
`aibridge-postgres`) surfaces usage, cost, intercept, and session data from
the Coder database, and the read-only Postgres role it uses is defined in
`sql/aibridge-grafana-ro.sql`.

The `coder-status` dashboard's "Observability Tools" row originally came from the
upstream coder/observability LGTM reference and checked components this demo does
not run (distributed Loki read/write/backend/canary, Grafana Agent, config
reloaders, Prometheus storage, CPU, RAM). It was replaced with `up` panels for
the components this stack actually runs: Prometheus, the single-binary Loki, and
Promtail. The same dashboard's "Workspace Builds" panel was repointed to
`coderd_workspace_latest_build_status` (the previous
`coderd_provisionerd_job_timings_seconds_count` has no series here), and its
"Postgres" panel to a boolean over `coderd_db_tx_duration_seconds` (there is no
postgres_exporter, so `pg_up` never existed).

## The logging path

1. A `promtail` DaemonSet runs on every node and tails the real container log
   files under `/var/log/pods` (containerd on EKS), discovering pods with the
   Kubernetes `pod` service-discovery role. It attaches `namespace`, `pod`,
   `container`, `app`, and `node_name` labels and pushes batches to
   `http://loki.monitoring.svc:3100/loki/api/v1/push`. There is no namespace
   filter, so all workload namespaces (`coder`, `coder-workspaces`, `gitlab`,
   `keycloak`, `monitoring`, `external-secrets`, and the rest) are captured.
2. `loki` runs as a single binary (`-target=all`, in-memory ring,
   `replication_factor: 1`) with filesystem object storage and a tsdb shipper on
   a 10Gi gp3 PVC. `auth_enabled` is false (single tenant, in-cluster only) and
   the compactor enforces ~168h retention.
3. `loki-datasource.yaml` provisions a Grafana datasource with uid exactly
   `loki`. The generated Coder dashboards reference that uid on their log panels,
   so creating it wires those panels to the live store. `isDefault` stays false
   so Prometheus remains the default datasource.
4. `ServiceMonitor/loki` and `ServiceMonitor/promtail` let Prometheus scrape both
   components' `/metrics`, so `up{job="loki"}` and `up{job="promtail"}` drive the
   `coder-status` dashboard's Loki and Promtail panels.

## Single sign-on (Keycloak)

Grafana logs in through the same Keycloak realm (`coder`) as Coder, so the demo
is one SSO. `scripts/setup-grafana-oidc.py` (idempotent) registers a confidential
OIDC client `grafana` (authorization-code + PKCE, redirect
`https://grafana.<BASE_DOMAIN>/login/generic_oauth`, full-path `groups`
mapper) and writes its client secret to AWS Secrets Manager at
`<CLUSTER_NAME>/observability/grafana-oauth`. ESO syncs that into the
`grafana-oauth` Secret, and Grafana reads it via the
`GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET` env (`grafana.envValueFrom`), so no secret
is in git.

The `[auth.generic_oauth]` block in `kube-prometheus-stack-values.yaml` maps
group membership to a Grafana org role
(`contains(groups[*], '/platform') && 'Admin' || 'Viewer'`): Platform
Engineering administers Grafana, everyone else is read-only `Viewer`. The local
admin login form is kept enabled as break-glass. See
`docs/as-built/55-observability.md` for the verified persona role mapping.

## Grafana admin credentials (ESO + AWS Secrets Manager)

The admin password is generated once and stored as JSON
`{"admin-user","admin-password"}` in AWS Secrets Manager at
`<CLUSTER_NAME>/observability/grafana`. The ESO `ClusterSecretStore`
`aws-secretsmanager` syncs it into the Kubernetes Secret `grafana-admin`
(namespace `monitoring`) via the ExternalSecret added to
`deploy/platform/external-secrets/secretstore-and-externalsecrets.yaml`. Grafana
consumes it through `admin.existingSecret`. The ESO IAM role only allows reading
`<CLUSTER_NAME>/*`, so this path is in policy. No password is committed to git.

Rotate by writing a new value to the ASM secret, then deleting the
`grafana-admin` Secret (ESO rebuilds it) or waiting for the 1h refresh, and
restart the Grafana pod to pick up the env value.

## Reproduce

```sh
. ~/.config/<CLUSTER_NAME>/env
export KUBECONFIG=./kubeconfig

# 1. Mirror the observability images into ECR.
bash scripts/mirror-images.sh

# 2. Enable Coder metrics + JSON logs (already in deploy/coder/values.yaml).
helm upgrade coder ~/.cache/helm/repository/coder_helm_2.34.0.tgz \
  --namespace coder --values deploy/coder/values.yaml --timeout 6m
kubectl -n coder rollout status deploy/coder

# 3. Grafana admin secret in ASM (generate; pass via a mode-600 file, not argv).
#    aws secretsmanager create-secret \
#      --name <CLUSTER_NAME>/observability/grafana \
#      --secret-string file:///path/to/grafana.json   # {"admin-user","admin-password"}

# 4. Namespace + ESO ExternalSecrets (Grafana admin + OIDC client secret).
#    First register the Keycloak client and publish its secret to ASM.
python3 scripts/setup-grafana-oidc.py
kubectl apply -f deploy/observability/namespace.yaml
kubectl apply -f deploy/platform/external-secrets/secretstore-and-externalsecrets.yaml
kubectl -n monitoring get externalsecret grafana-admin grafana-oauth   # Ready=SecretSynced

# 5. Install the stack.
helm install kps ~/.cache/helm/repository/kube-prometheus-stack-86.2.0.tgz \
  --namespace monitoring --values deploy/observability/kube-prometheus-stack-values.yaml --timeout 8m

# 6. Coder scrape target, Grafana Ingress, dashboards.
kubectl apply -f deploy/observability/coder-metrics.yaml
kubectl apply -f deploy/observability/grafana-ingress.yaml
kubectl apply -f deploy/observability/dashboards-coder.yaml

# 7. Logging stack: Loki, Promtail, and the Grafana Loki datasource.
#    The images were mirrored by step 1 (they are in scripts/images.txt).
kubectl apply -f deploy/observability/loki.yaml
kubectl -n monitoring rollout status deploy/loki
kubectl apply -f deploy/observability/promtail.yaml
kubectl apply -f deploy/observability/loki-datasource.yaml
kubectl -n monitoring rollout status ds/promtail
```

To regenerate `dashboards-coder.yaml` from upstream, extract the
`coder-dashboard-*` ConfigMaps from
`https://raw.githubusercontent.com/coder/observability/main/compiled/resources.yaml`,
relabel them with `grafana_dashboard: "1"`, set namespace `monitoring`, and drop
the `agent-boundaries` (Loki-only) dashboard.

## Verify

```sh
# Coder target UP
kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090 &
curl -s 'http://localhost:9090/api/v1/query?query=up{job="coder-metrics"}'

# Grafana over HTTPS (valid TLS) + datasource + dashboards (admin from ASM)
GPW=$(kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d)
curl -s -o /dev/null -w '%{http_code} ssl=%{ssl_verify_result}\n' https://grafana.<BASE_DOMAIN>/login
curl -s -u "admin:$GPW" 'https://grafana.<BASE_DOMAIN>/api/search?type=dash-db&query=Coder'

# Keycloak SSO button + redirect (client_id=grafana, PKCE)
curl -s https://grafana.<BASE_DOMAIN>/login | grep -o '"oauth":{[^}]*}'
curl -s -o /dev/null -D - https://grafana.<BASE_DOMAIN>/login/generic_oauth | grep -i '^location:'

# Loki ingesting logs (labels + a sample query through a port-forward)
kubectl -n monitoring port-forward svc/loki 3100:3100 &
curl -s 'http://localhost:3100/loki/api/v1/labels'
curl -s -G 'http://localhost:3100/loki/api/v1/query_range' \
  --data-urlencode 'query={namespace="coder"}' --data-urlencode 'limit=1'

# Loki + Promtail scraped by Prometheus (both 1)
curl -s 'http://localhost:9090/api/v1/query?query=up{job="loki"}'
curl -s 'http://localhost:9090/api/v1/query?query=up{job="promtail"}'

# Loki datasource present in Grafana with uid loki
curl -s -u "admin:$GPW" 'https://grafana.<BASE_DOMAIN>/api/datasources/uid/loki'
```
