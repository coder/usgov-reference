# Namespace sidecar-injection plan

Istio sidecar injection is opt-in per namespace via the `istio-injection=enabled`
label (a pod gets a sidecar only after the namespace is labeled AND the pod is
recreated). This file is the source of truth for which namespaces join the mesh,
in what order, and which stay out and why.

Injection happens in Phase 6 of the rollout, AFTER the ingress cutover and AFTER
`peerauthentication-permissive.yaml` is applied. Backends do not need a sidecar
for the ingress gateway to route to them, so ingress is cut over first; mTLS
between the gateway and a backend only begins once that backend is injected, and
PERMISSIVE keeps both paths working throughout.

## Inject (in this order, lowest blast radius first)

| Order | Namespace | Why this order | Validate after restart |
|------|-----------|----------------|------------------------|
| 1 | `keycloak` | Single pod. RDS is app-TLS (DestinationRule DISABLE). Mgmt port 9000 covered by probe rewrite. | Login flow, Account Console cookies still `Secure; SameSite=None`, probes green |
| 2 | `coder` | Control plane talks to RDS and to workspaces via the gateway. Metrics on 2112. | Dashboard loads, OIDC login, a workspace build succeeds, `coder-metrics` still scraped |
| 3 | `gitlab` | Omnibus: most ports are intra-pod on localhost (not intercepted), but git-over-http and websockets need a careful look. | `git clone`/push over https, CI job, web terminal, container registry push |

Label and roll a namespace:

```sh
kubectl label namespace <ns> istio-injection=enabled --overwrite
kubectl rollout restart deployment -n <ns>     # recreate pods so sidecars inject
kubectl rollout status   deployment -n <ns>
istioctl proxy-status                           # all entries SYNCED
```

## Do NOT inject (intentional exceptions)

| Namespace | Reason | Revisit |
|-----------|--------|---------|
| `coder-workspaces` | HIGHEST RISK. Workspace pods are created dynamically by Coder and run the Coder agent (DERP/tunnel networking, many outbound connections, web terminals). Sidecar iptables capture can break agent connectivity, and these pods are not long-lived control-plane services. Workspace-to-Coder traffic transits the ingress gateway (external path), not direct pod-to-pod, so excluding this namespace does not leave an unencrypted east-west gap that STRICT would otherwise cover. | After the rest of the mesh is STRICT and stable, pilot a SINGLE workspace with injection (`pod.metadata` annotation or a labeled namespace) and validate agent tunnels, web terminals, and app proxying before considering namespace-wide injection. |
| `monitoring` | Prometheus scrape paths to meshed pods need mesh-aware scraping under STRICT. This is owned by the observability workstream (deploy/observability) and is a later, coordinated step. Leaving it out keeps scraping over plain text during the core rollout. | Observability workstream injects it together with ServiceMonitor/PodMonitor changes and Istio metrics merging. |
| `external-secrets` | ESO controllers talk to the Kubernetes API and to AWS (IRSA egress) and run admission webhooks; no in-mesh east-west value, real webhook risk. | Only if a concrete mTLS requirement appears. |
| `kube-system`, `kube-node-lease`, `kube-public`, `default` | System namespaces and cluster add-ons (AWS LB Controller, VPC CNI, CoreDNS). Never inject. | Never. |
| `istio-system` | Control plane. istiod and the gateway are managed by the IstioOperator, not by namespace injection. | n/a |

## Highest-risk item, called out explicitly

`coder-workspaces` injection is the single riskiest decision in this adoption.
Recommendation: EXCLUDE it initially (leave unlabeled). The mesh still gets
mTLS across the platform control plane (coder, keycloak, gitlab) and the value
of injecting ephemeral workspace pods is low relative to the risk to agent
networking. Treat workspace injection as a separate, later experiment, not part
of the STRICT cutover.
