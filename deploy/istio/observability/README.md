# deploy/istio/observability

Istio mesh observability UIs for the GovCloud Coder demo: **Kiali** (the mesh
service-graph console with mTLS padlock badges) and the standard **Istio Grafana
dashboards**, wired into the existing in-cluster kube-prometheus-stack
(Prometheus + Grafana in ns `monitoring`). Everything is air-gapped: the only
container image is mirrored to ECR.

These manifests have been applied to the live demo cluster and verified (see
[Verification](#verification)). They add to the existing Istio install
(`deploy/istio/`) and observability stack (`deploy/observability/`) without
modifying either.

## What is here

| Path | Purpose |
|------|---------|
| `servicemonitor-istiod.yaml` | ServiceMonitor scraping istiod control-plane metrics on the `istiod` Service port `http-monitoring` (15014). Excludes the `istiod-revision-tag-default` Service so istiod is scraped once. |
| `podmonitor-istio-proxies.yaml` | PodMonitor scraping every Istio proxy's merged telemetry at `:15020/stats/prometheus` (the ingress gateway now; app sidecars once injected). Official Istio "Prometheus with Operator" relabeling. |
| `dashboards-istio.yaml` | The five standard Istio dashboards as Grafana ConfigMaps (`grafana_dashboard: "1"`), sourced from the istio `release-1.30` tree. |
| `kiali-server-values.yaml` | Helm values for the `kiali-server` chart 2.26.0 (image overridden to the ECR mirror, Prometheus/Grafana URLs, Keycloak OpenID SSO, web_root `/kiali`, public URL settings). |
| `kiali.yaml` | GENERATED rendered Kiali server manifest (from the chart + values). Server only, no operator. |
| `externalsecret-kiali-oauth.yaml` | ESO ExternalSecret that syncs the Kiali OIDC client secret from AWS Secrets Manager into the `kiali` Secret (key `oidc-secret`) that Kiali reads for OpenID login. |
| `virtualservice-kiali.yaml` | Routes `kiali.<BASE_DOMAIN>` through `istio-system/public-gateway` to the `kiali` Service (20001). Manifest only; no DNS change. |

The Keycloak OIDC client (`kiali`, realm `coder`) is provisioned by
`scripts/setup-kiali-oidc.py`, which also publishes the client secret to AWS
Secrets Manager for ESO to sync. No secret value is committed to git.

Apply everything with `scripts/setup-istio-observability.sh`
(`--verify` runs post-apply checks; `--render-kiali` regenerates `kiali.yaml`).

## Versions and air gap

- **Kiali v2.26.0** (chart `kiali-server` 2.26.0). v2.26 is the Kiali line Istio
  1.30 certifies: the istio-1.30.1 Kiali addon ships `quay.io/kiali/kiali:v2.26`
  and labels it v2.26.0.
- Image mirrored to ECR via `scripts/images.txt` + `scripts/mirror-images.sh`:
  `<ACCOUNT_ID>.dkr.ecr.us-gov-west-1.amazonaws.com/quay/kiali/kiali:v2.26.0`
  (digest `sha256:59cce98a9811ce53ff3da771225d1df00f0ca4ae0819311291ae7316349a13e9`).
- Dashboards are plain JSON ConfigMaps; no image needed. Helm renders Kiali
  client-side, so the cluster only ever pulls the ECR image.

## Metrics path

```
istiod        :15014/metrics            --[ServiceMonitor istio-component-monitor]--+
istio-proxy   :15020/stats/prometheus   --[PodMonitor envoy-stats-monitor]----------+--> kps Prometheus
                                                                                     |        (ns monitoring)
                                                                                     v
                                                              Istio Grafana dashboards + Kiali
```

- The kps Prometheus operator selects ServiceMonitors/PodMonitors cluster-wide
  (`*SelectorNilUsesHelmValues: false` with empty selectors), so the `release:
  kps` label on these monitors is belt-and-suspenders, matching
  `deploy/observability/coder-metrics.yaml`.
- The proxy PodMonitor is annotation-driven: each proxy pod carries
  `prometheus.io/scrape=true`, `prometheus.io/path=/stats/prometheus`,
  `prometheus.io/port=15020`, and the relabeling rewrites the scrape target to
  the merged port. This is why app sidecars are picked up automatically once
  their namespaces are injected, with no manifest change.
- Kiali reads Prometheus at
  `http://kps-kube-prometheus-stack-prometheus.monitoring.svc:9090` and links to
  Grafana at `http://kps-grafana.monitoring.svc:80` (browser links use
  `https://grafana.<BASE_DOMAIN>`).

## Reaching Kiali

- **Now (no DNS change):** port-forward.
  ```sh
  kubectl -n istio-system port-forward svc/kiali 20001:20001
  # open http://localhost:20001/kiali
  ```
- **Through the gateway:** the VirtualService is live and `kiali.<BASE_DOMAIN>`
  already matches the gateway's `*.<BASE_DOMAIN>` server. Once the
  orchestrator adds an additive Route53 record pointing
  `kiali.<BASE_DOMAIN>` at the Istio gateway NLB, Kiali is reachable at
  **https://kiali.<BASE_DOMAIN>/kiali**. Validate routing before the DNS cut
  with `--resolve` against a gateway NLB public IP. Anonymous API access is
  denied (401) and unauthenticated users are bounced to Keycloak:
  ```sh
  GIP=$(aws ec2 describe-network-interfaces \
    --filters "Name=description,Values=ELB net/k8s-istiosys-istioing-bf7bdca8c8/*" \
    --query 'NetworkInterfaces[0].Association.PublicIp' --output text)
  # API without a session is rejected (expect 401, NOT 200 with data):
  curl -sSk --resolve kiali.<BASE_DOMAIN>:443:$GIP \
    -o /dev/null -w '%{http_code}\n' \
    https://kiali.<BASE_DOMAIN>/kiali/api/namespaces
  # The login redirect points at the Keycloak authorize endpoint (expect 302):
  curl -sSk --resolve kiali.<BASE_DOMAIN>:443:$GIP -D - -o /dev/null \
    https://kiali.<BASE_DOMAIN>/kiali/api/auth/openid_redirect | grep -i location
  ```

## Auth

Kiali uses `auth.strategy: openid` (Keycloak OpenID Connect SSO). **Anonymous
access is disabled.** Kiali is fronted by the same Keycloak realm (`coder`) that
fronts Coder, Grafana, and GitLab.

### How it is wired

- **Keycloak client.** `scripts/setup-kiali-oidc.py` creates/updates a
  confidential OIDC client `kiali` in realm `coder` (authorization-code flow +
  PKCE S256), with redirect URIs `https://kiali.<BASE_DOMAIN>/kiali/*` and
  the bare `https://kiali.<BASE_DOMAIN>/kiali`, plus the shared full-path
  `groups` mapper. The script is idempotent and mirrors
  `scripts/setup-grafana-oidc.py` / `scripts/setup-gitlab-oidc.py`.
- **Client secret (no git secret).** The script publishes the client secret to
  AWS Secrets Manager at `<CLUSTER_NAME>/observability/kiali-oauth` as JSON
  `{"oidc-secret": "..."}`. `externalsecret-kiali-oauth.yaml` syncs it (ESO,
  ClusterSecretStore `aws-secretsmanager`) into the Secret `kiali` in
  `istio-system` under key `oidc-secret`. That is exactly the Secret name and
  key Kiali v2.26 OpenID reads (the kiali Deployment mounts it at
  `/kiali-secret`). ASM is the source of truth; ESO refreshes hourly.
- **Kiali config** (`kiali-server-values.yaml` / `kiali.yaml`):
  - `auth.openid.issuer_uri: https://auth.<BASE_DOMAIN>/realms/coder`
  - `auth.openid.client_id: kiali`
  - `auth.openid.username_claim: preferred_username` (labels the user in the UI)
  - `auth.openid.scopes: [openid, profile, email]`
  - `auth.openid.disable_rbac: true`
  - `deployment.view_only_mode: true`
  - `server.web_fqdn/web_port/web_schema` set to the public
    `https://kiali.<BASE_DOMAIN>:443` endpoint.

### Why `disable_rbac: true` (and `view_only_mode: true`)

Kiali's `openid` strategy can enforce per-user Kubernetes RBAC only when the
cluster API server itself trusts the same OIDC issuer (so it accepts the user's
Keycloak token). This GovCloud EKS API server is not integrated with Keycloak as
an OIDC token issuer, so per-user RBAC is not available. `disable_rbac: true` is
the documented setting for that case: any user who completes the Keycloak login
may view the mesh, and Kiali queries the cluster with its own ServiceAccount.
Because that shared ServiceAccount could otherwise mutate Istio config through
Kiali's wizards, the install pairs it with `deployment.view_only_mode: true`, so
the console is strictly read-only. This matches the demo goal: any authenticated
realm user may view the mesh, nobody can change it from Kiali.

The 32-byte `login_token.signing_key` the chart renders keeps Kiali on the
authorization-code flow, which requires the `oidc-secret` Secret above.

### Public URL settings (OpenID redirect)

`server.web_fqdn: kiali.<BASE_DOMAIN>`, `server.web_port: "443"`, and
`server.web_schema: https` are required. Without them Kiali builds the OpenID
`redirect_uri` from its internal listen port (`:20001`), which Keycloak would
reject, breaking login. With them, Kiali emits
`redirect_uri=https://kiali.<BASE_DOMAIN>/kiali`, which matches the
registered redirect URI.

### Re-running / rotating

Re-run `scripts/setup-kiali-oidc.py` to reconcile the client and re-publish the
current secret; it does not rotate the Keycloak secret on each run. After a
secret change, ESO re-syncs within the refresh interval (or force it with
`kubectl -n istio-system annotate externalsecret kiali-oauth force-sync=$(date +%s) --overwrite`),
then `kubectl -n istio-system rollout restart deploy/kiali`.

## What to show in the demo

- **Istio Control Plane dashboard** (Grafana): works immediately. It renders
  istiod data (`pilot_xds`, proxy convergence, push counts) the moment
  Prometheus scrapes istiod.
- **Kiali mesh graph**: the infrastructure view already shows istiod, the ingress
  gateway, Prometheus, Grafana, and Kiali. The traffic graph shows live edges for
  whatever flows through the gateway.
- **mTLS padlocks**: Kiali draws a padlock on edges carrying sidecar-to-sidecar
  mTLS, and the Istio Service/Workload dashboards include `(🔐mTLS)` legend
  series. These appear once application namespaces are **sidecar-injected** and
  generate traffic. With no injected namespaces yet, there is no
  sidecar-to-sidecar traffic, so the Mesh/Service/Workload dashboards and the
  padlocked Kiali edges are sparse by design. This is mesh traffic, not AI
  traffic, so the placeholder-Anthropic / sparse-AI-traffic situation elsewhere
  in the demo is irrelevant here.

## Verification

Captured against the live cluster after apply:

- Prometheus targets `up=1`: istiod (`:15014`) and both ingress gateway proxies
  (`:15020`). `istio_requests_total` is populated from the gateway
  (`source_workload=istio-ingressgateway`); istiod metrics (`pilot_xds`,
  `pilot_proxy_convergence_time_bucket`, `galley_validation_passed`) are present.
- Grafana lists all five Istio dashboards; the Control Plane dashboard's
  `pilot_xds` query returns data through Grafana's Prometheus datasource.
- Kiali v2.26.0 pod is `Running`; `/kiali/healthz` returns 200 and the pod log
  reports `Using authentication strategy [openid]` and `Using OpenID
  auto-discovery from provider`.
- **Anonymous access is denied.** With no session, `/kiali/api/namespaces`,
  `/kiali/api/istio/status`, and `/kiali/api/config` return **401** (previously
  200 with data), and `/kiali/api/auth/info` reports `strategy: openid`. Verified
  both via port-forward and through every gateway NLB IP with `--resolve`.
- **Unauthenticated users are redirected to Keycloak.**
  `/kiali/api/auth/openid_redirect` returns 302 to
  `https://auth.<BASE_DOMAIN>/realms/coder/protocol/openid-connect/auth`
  with `client_id=kiali`, `response_type=code`, `code_challenge_method=S256`
  (PKCE), and `redirect_uri=https://kiali.<BASE_DOMAIN>/kiali`. Following
  that authorize URL returns the realm `coder` login page (HTTP 200, no
  `invalid redirect_uri` error), confirming the client and redirect URI are
  accepted end-to-end.
- **Browser login confirmed.** A Keycloak realm user completed the full
  interactive login and landed in the Kiali mesh graph (read-only, per
  `view_only_mode: true`), so the end-to-end SSO loop, including the credential
  prompt and callback, is verified, not just the headless redirect checks above.
