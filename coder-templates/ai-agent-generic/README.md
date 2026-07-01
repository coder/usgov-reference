# ai-agent-generic

Generic, language-agnostic workspace for the Coder Agents path on the GovCloud
demo. Runs in a Kubernetes pod on EKS as plain, hardened compute. It is the
default target a server-side Coder Agent uses when a task is not
language-specific: the AI runs on the control plane and operates the workspace
remotely (read_file, write_file, execute).

See `../_shared/README.md` for the shared EKS pattern and invariants.

## Why this template is different

| Aspect | This template | Other family members |
|---|---|---|
| Privilege escalation | disabled (`no_new_privs`) | enabled (for sudo apt) |
| Startup installs | none (PATH only) | best-effort apt toolchains |
| LLM wiring | none | none |
| Egress needed | GitLab plus coderd only | plus apt/toolchain endpoints |

Because the workspace never calls an LLM, it is a candidate for a tighter
egress policy (git plus the Coder control plane only), enforced at the platform
layer with a NetworkPolicy or Istio rule. If that policy is applied, any extra
tooling (including the `code-server` binary) must be prebaked into an
in-boundary image rather than fetched at startup.

## Tooling

- Whatever the base image ships (`enterprise-base`: git, curl, a shell).
- `code-server` (VS Code in the browser, subdomain app) for human inspection.
- `git-clone` (optional) for the assigned repo.

A richer prebaked generic image (git, build-essential, Python, Node, ripgrep,
jq) is the recommended end state so the server-side agent has a full toolbox
without startup downloads. See the WS-25 handoff for the root mirror/build
TODO.

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

> Generic agent runtime workspace: plain compute (git, shell, prebaked tools)
> with no in-workspace LLM tooling. Default target for server-side Coder Agents
> tasks that are not language-specific.

Applied to the template by the root operator with `coder templates edit
ai-agent-generic --description "..."`. This is the fallback Coder Agents selects
when no language-specific template matches. Push/build/test/cleanup commands are
in `docs/swarm/workstreams/WS-25-templates.md`.
