# GitLab CI runners (Istio-safe)

GitLab Runner for the <CLUSTER_NAME> GovCloud demo. A group runner registered
to `<GITLAB_GROUP>` serves any project in the group with Kubernetes executor
jobs (Kaniko image builds, Coder template pushes, or any other CI workload).

- **GitLab**: `https://gitlab.<BASE_DOMAIN>` (CE 19.0.1, ns `gitlab`).
- **Container Registry**: `https://registry.<BASE_DOMAIN>` (bundled with
  GitLab CE, fronted by the Istio gateway).
- **Coder**: `https://dev.<BASE_DOMAIN>` (orgs `coder`, `alpha`, `bravo`).
- **Runner namespace**: `gitlab-runner` (NOT in the Istio mesh).

## Why this is Istio-safe

Mesh-wide STRICT mTLS is enforced (`istio-system/default` PeerAuthentication).
The meshed namespaces are `coder`, `keycloak`, and `gitlab`. A plain-text
connection from a non-meshed pod to a meshed Service (for example
`gitlab.gitlab.svc:80`) is refused under STRICT.

This deployment avoids that hop entirely:

| Concern | Decision |
|---|---|
| Sidecar lifecycle vs. short-lived CI pods | `gitlab-runner` namespace is kept **out** of the mesh (`istio-injection: disabled`). |
| Runner -> GitLab | Runner registers/polls the **external** URL `https://gitlab.<BASE_DOMAIN>`. |
| CI job -> Coder | The `push-template` job uses the **external** URL `https://dev.<BASE_DOMAIN>`. |
| CI job -> Container Registry | The `build-images` job pulls/pushes over the **external** URL `https://registry.<BASE_DOMAIN>`. |
| Where mTLS happens | All three external URLs resolve to the **Istio ingress gateway** NLB; the gateway is in the mesh and performs mTLS to the `gitlab`/`coder` workloads. |

So nothing the runner or its jobs do requires a plain-text hop to a meshed
Service. The secured (mTLS) hop still happens, just at the gateway. Pods in a
non-meshed namespace reach the gateway NLB via hairpin, the same path Coder
workspace agents already use.

## Egress and image sourcing

The `gitlab-runner` namespace has internet egress (verified:
`registry.access.redhat.com`, `dl.fedoraproject.org`, `rpm.nodesource.com`,
`download.rockylinux.org`, `starship.rs`), so this is **not** a strict air gap.
Kaniko pulls the UBI base and `dnf`-installs directly. The runner manager,
executor helper, Kaniko, and coder job images are still ECR mirrors
(`scripts/images.txt` + `scripts/mirror-images.sh`) for speed and reliability:

| Role | ECR image |
|---|---|
| Runner manager | `docker-hub/gitlab/gitlab-runner:v19.0.1` |
| Executor helper | `docker-hub/gitlab/gitlab-runner-helper:x86_64-v19.0.1` |
| `build-images` job image | `gcr/kaniko-project/executor:v1.24.0-debug` |
| `push-template` job image | `ghcr/coder/coder:v2.34.0` (ships `/bin/sh` + the `coder` CLI) |

The runner + helper versions are pinned to match the GitLab CE server (19.0.1).
Each job overrides its image entrypoint (`entrypoint: [""]`) so the runner's
shell runs the job script.

## Container Registry

The bundled GitLab Container Registry is enabled in `deploy/gitlab/`
(`statefulset.yaml`, `service.yaml`): the registry speaks plain HTTP on `:5050`
and trusts the gateway's forwarded-proto header, and TLS terminates upstream at
the NLB / Istio gateway using the existing `*.<BASE_DOMAIN>` ACM cert.
`deploy/gitlab/virtualservice-registry.yaml` routes
`registry.<BASE_DOMAIN>` through the shared `public-gateway` to the
`gitlab` Service `:5050`.

> Images built in CI live in a **private** project registry. A workspace
> template that consumes them needs a `kubernetes.io/dockerconfigjson` pull
> Secret in `coder-workspaces` (passed via `image_pull_secret`). Template
> import (`terraform plan`) does not pull images, so import works without it.

## Files

| File | Purpose |
|---|---|
| `namespace.yaml` | `gitlab-runner` namespace, explicitly out of the mesh. |
| `externalsecret.yaml` | ESO `ExternalSecret` syncing the runner auth token from ASM (`<CLUSTER_NAME>/gitlab/runner`) into the `gitlab-runner-auth` Secret. |
| `values.yaml` | Helm values: ECR images, external `gitlabUrl`, Kubernetes executor, least-privilege RBAC, per-job resource override allowances for the Kaniko build. |

The bundled Container Registry that the build job pushes to lives in
`deploy/gitlab/` (`statefulset.yaml`, `service.yaml`,
`virtualservice-registry.yaml`).

## Deploy

Prereqs: `. ~/.config/<CLUSTER_NAME>/env`, `export KUBECONFIG=...`,
`export PATH="$HOME/.local/bin:$PATH"`, and the runner/helper/Kaniko/coder
images mirrored.

```sh
# 1. Mirror images (idempotent).
./scripts/mirror-images.sh --file scripts/images.txt

# 2. Recreate the GitLab project + CI variables + Coder token + group runner
#    token. Deletes root/coder-templates and creates <GITLAB_GROUP>/coder-templates.
#    (Writes the runner auth token to ASM <CLUSTER_NAME>/gitlab/runner.)
python3 scripts/setup-gitlab-ci-runners.py

# 3. Namespace + ESO secret (materializes gitlab-runner-auth from ASM).
kubectl apply -f deploy/gitlab-runner/namespace.yaml
kubectl apply -f deploy/gitlab-runner/externalsecret.yaml

# 4. Re-sync the rotated runner token and (re)install/roll the runner so it
#    re-registers with the new GROUP token.
kubectl -n gitlab-runner delete secret gitlab-runner-auth --ignore-not-found
kubectl -n gitlab-runner annotate externalsecret gitlab-runner-auth \
  force-sync=$(date +%s) --overwrite
helm repo add gitlab https://charts.gitlab.io
helm repo update gitlab
helm upgrade --install gitlab-runner gitlab/gitlab-runner \
  --version 0.89.1 --namespace gitlab-runner \
  -f deploy/gitlab-runner/values.yaml

# 5. Confirm the runner is online.
kubectl -n gitlab-runner get pods
#    GitLab UI: group <GITLAB_GROUP> > Build > Runners (green), or project
#    <GITLAB_GROUP>/coder-templates > Settings > CI/CD > Runners.
```

## Verify runner is online

```sh
# Pod is running.
kubectl -n gitlab-runner get pods

# Gateway-served registry responds (expect 401 Bearer challenge).
curl -sS -D - -o /dev/null https://registry.<BASE_DOMAIN>/v2/
```

Trigger a CI pipeline on a `<GITLAB_GROUP>` project to confirm job pods are
scheduled and complete successfully.

## Secret handling

- **Runner authentication token** (`glrt-...`): source of truth in AWS Secrets
  Manager (`<CLUSTER_NAME>/gitlab/runner`), synced into the cluster by ESO.
  Never committed. A **group** runner token on `<GITLAB_GROUP>` serves
  `<GITLAB_GROUP>/coder-templates`.
- **Coder CI token** (`CODER_SESSION_TOKEN`): a rotating Coder API token stored
  only as a masked + protected GitLab CI/CD variable on the project. Never
  committed.
- Re-run `scripts/setup-gitlab-ci-runners.py` to rotate the Coder token and
  reconcile the project/variables/runner token in place.

> Coder caps token lifetime at the server's `max_token_lifetime`. This deploy
> sets both `CODER_MAX_TOKEN_LIFETIME` and `CODER_MAX_ADMIN_TOKEN_LIFETIME` to
> `8760h` (1 year) in `deploy/coder/values.yaml`, so the admin-minted CI token
> lasts a year. The setup script issues the token at that maximum and is safe
> to re-run on a schedule to rotate it. Admin-user tokens are governed by the
> separate `CODER_MAX_ADMIN_TOKEN_LIFETIME`; both caps must be raised for an
> admin-minted token to exceed the 168h default.
