# platform-engineer

Platform and DevOps engineering workspace for the GovCloud demo. Runs in a
Kubernetes pod on EKS as plain compute for the Coder Agents path: the AI runs
server-side, so this template has no AI Gateway wiring, no LLM keys, and no
agent harness.

See `../_shared/README.md` for the shared EKS pattern and invariants.

## Tooling

Provisioned best-effort at startup (tolerant: a failed install never fails the
build):

- `awscli`, `jq`, `unzip`, `ripgrep` (from apt)
- `kubectl` (latest stable, official `dl.k8s.io`)
- `helm` (official `get-helm-3`)
- `terraform` (HashiCorp releases, pinned)
- `code-server` (VS Code in the browser, subdomain app)

The kubectl/helm/terraform downloads reach external endpoints. In a fully
air-gapped boundary those may be blocked; the installs are tolerant and the
workspace still comes up. For an air-gapped demo, bake the binaries into a
custom workspace image and set `workspace_image`; the download block then
becomes a no-op. See the WS-25 handoff for the root mirror/build TODO.

## Parameters

| Parameter | Type | Mutable | Default | Options |
|---|---|---|---|---|
| `cpu` | number | yes | `4` | 2, 4, 8 |
| `memory` (GB) | number | yes | `8` | 4, 8, 16 |
| `disk_size` (GB) | number | no | `20` | 10, 20, 50 |
| `git_repo` | string | no | `""` | optional repo to clone on start |

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `namespace` | `coder-workspaces` | namespace for workspace pods |
| `workspace_image` | ECR-mirrored `enterprise-base` | workspace container image |
| `use_kubeconfig` | `false` | use a host kubeconfig instead of in-cluster config |

## Git auth

`data "coder_external_auth" "gitlab"` (`id = "gitlab"`) makes GitLab login
required. After the owner completes the in-boundary OAuth flow, the agent git
credential helper injects a short-lived token for clone/fetch/push to
`gitlab.<BASE_DOMAIN>`. No PATs or SSH keys live in the workspace.

## Routing description

> Platform/DevOps engineering workspace: kubectl, Helm, Terraform, AWS CLI, jq.
> Use for infrastructure-as-code, Kubernetes operations, and cloud platform
> work.

Applied to the template by the root operator with `coder templates edit
platform-engineer --description "..."` so Coder Agents auto-selects it for
platform work. Push/build/test/cleanup commands are in
`docs/swarm/workstreams/WS-25-templates.md`.
