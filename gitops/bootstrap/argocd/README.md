# bootstrap/argocd/ - Argo CD install values

This directory holds the Argo CD Helm values file used for the bootstrap
install. Chart `argo/argo-cd` **10.0.1** (appVersion **v3.4.4**).

## Install command

```sh
helm install argo-cd argo/argo-cd --version 10.0.1 -n argocd \
  --create-namespace -f gitops/bootstrap/argocd/values.yaml
```

The Helm-native apply is not subject to the kubectl client-side CRD
annotation limit, so `--server-side` is unnecessary for a clean install.

## What `values.yaml` configures

- Image repository overrides pointing all Argo CD components at the
  private ECR mirror paths (no pull-through cache in GovCloud):
  `<ACCOUNT_ID>.dkr.ecr.us-gov-west-1.amazonaws.com/quay/argoproj/argocd:v3.4.4`
  for the shared core image and
  `<ACCOUNT_ID>.dkr.ecr.us-gov-west-1.amazonaws.com/docker-hub/library/redis:8.2.3-alpine`
  for the bundled Redis.
- `application.resourceTrackingMethod: annotation` (MANDATORY; set before
  any Helm release is adopted; annotation tracking prevents Argo from
  mutating immutable `app.kubernetes.io/instance` label selectors in
  Deployment and StatefulSet specs).
- `admin.enabled: true`: local-admin break-glass access.
- `server.service.type: ClusterIP` and `server.ingress.enabled: false`:
  no external ingress for Argo during bootstrap. Admin access is via
  `kubectl port-forward` only.
- Dex disabled (no SSO on first install) and notifications disabled
  to keep the bootstrap footprint minimal.

## Break-glass admin access

```sh
kubectl -n argocd port-forward svc/argocd-server 8080:443
argocd login localhost:8080 --insecure --username admin \
  --password "$(kubectl -n argocd get secret argocd-initial-admin-secret \
    -o jsonpath='{.data.password}' | base64 -d)"
```

## Deferred configuration

- Keycloak OIDC SSO for the Argo UI and API, and the matching RBAC policy
  mapping your platform-admin group to the Argo `admin` role. Configure
  after Keycloak is healthy and the realm is seeded.
- A global `''/Secret` entry in `resource.exclusions` was deliberately
  omitted; the ESO-owned Secret guardrail is enforced per-Application via
  `prune: false` + `orphanedResources.warn` instead.

## Bootstrap dependency order

1. ECR image mirror: run `scripts/mirror-images.sh` after adding the Argo
   images to `scripts/images.txt`.
2. GitLab project `<GITLAB_GROUP>/<CLUSTER_NAME>` created in-cluster; a
   `read_repository` deploy token minted via the GitLab API.
3. Argo CD installed via Helm (`helm install` command above).
4. Repo credential provisioned as a plain labeled Secret
   (`argocd.argoproj.io/secret-type: repository`) in the `argocd`
   namespace, so Argo can reach the in-cluster GitLab before ESO is
   managing secrets. A plain Secret avoids an ESO bootstrap dependency loop.
5. Root app applied: `kubectl apply -f gitops/bootstrap/root-app.yaml`.
6. Child Applications created (not yet synced). Run `argocd app diff <name>`
   per workload before any sync.

## Source URL

All Applications point at the in-cluster GitLab URL:
`http://gitlab.gitlab.svc.cluster.local/<GITLAB_GROUP>/<CLUSTER_NAME>.git`

This keeps git traffic in-boundary with no egress to github.com or other
external sources on the reconcile path.
