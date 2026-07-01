# data-scientist

Python data-science workspace for the GovCloud demo. Runs in a Kubernetes pod
on EKS as plain compute for the Coder Agents path: the AI runs server-side, so
this template has no AI Gateway wiring, no LLM keys, and no agent harness.

See `../_shared/README.md` for the shared EKS pattern and invariants.

## Tooling

Provisioned best-effort at startup via `sudo apt-get` plus `pipx` on the
`enterprise-base` image (tolerant: a failed install never fails the build):

- `python3`, `python3-venv`, `python3-pip`, `pipx`
- `jupyterlab` (via pipx), launched on `127.0.0.1:8888` and exposed as the
  `JupyterLab` subdomain app
- `ripgrep`, `jq`
- `code-server` (VS Code in the browser, subdomain app)

JupyterLab runs with its own token and password disabled because Coder enforces
auth at the app boundary (owner share). For a fully air-gapped demo, bake the
Python stack and common data libraries into a custom workspace image and set
`workspace_image`; the install becomes a no-op while the launch still works.
See the WS-25 handoff for the root mirror/build TODO.

## Apps

| App | Access | Notes |
|---|---|---|
| `JupyterLab` | subdomain | `coder_app.jupyter`, proxied to localhost:8888 |
| `code-server` | subdomain | VS Code in the browser |

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

> Data science workspace: Python 3, JupyterLab, pip/venv. Use for notebooks,
> data analysis, and ML prototyping.

Applied to the template by the root operator with `coder templates edit
data-scientist --description "..."` so Coder Agents auto-selects it for data
work. Push/build/test/cleanup commands are in
`docs/swarm/workstreams/WS-25-templates.md`.
