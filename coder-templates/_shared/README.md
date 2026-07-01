# Workspace template family (shared notes)

These notes define the common shape every Phase-2 workspace template in this
directory follows. The templates are deliberately self-contained (Coder uploads
only the pushed directory), so each `main.tf` repeats the shared blocks rather
than importing them. This file is the single description of the invariants so
the family stays coherent.

## Family members

| Template | Purpose | Base image (default) | Privilege escalation |
|---|---|---|---|
| `cpp-engineer` | C / C++ systems development | `enterprise-base` | enabled (sudo apt) |
| `java-engineer` | Java / JVM development | `enterprise-base` | enabled (sudo apt) |
| `platform-engineer` | DevOps / IaC / Kubernetes ops | `enterprise-base` | enabled (sudo apt) |
| `data-scientist` | Python / JupyterLab notebooks | `enterprise-base` | enabled (sudo apt) |
| `ai-agent-generic` | Plain compute for server-side Coder Agents | `enterprise-base` | disabled (hardened) |

All five share one EKS pod pattern. They differ only by: the toolchain the
startup script provisions, an optional extra `coder_app`, the routing
`description`, the icon, and (for `ai-agent-generic`) a hardened security
posture.

## What plain compute means here

For the Coder Agents path the workspace is plain compute. The agent
intelligence runs on the control plane (server-side `chatd`), so these
templates intentionally do NOT include:

- the `claude-code` / `aibridge` modules,
- any `ANTHROPIC_*` / AI Gateway env,
- `coder_ai_task` / `data.coder_task`,
- any LLM API key injection.

A server-side Coder Agent operates the workspace remotely (read_file,
write_file, execute), so the workspace only needs good developer tooling plus
git access to the in-boundary GitLab.

## Shared EKS pattern (adapted from `claude-code`)

- Providers: `coder` (>= 2.13.0) and `hashicorp/kubernetes` (>= 2.23).
- Variables: `use_kubeconfig` (default false), `namespace`
  (default `coder-workspaces`), `workspace_image` (default the ECR-mirrored
  `enterprise-base`).
- Required GitLab external auth: `data "coder_external_auth" "gitlab"` with
  `id = "gitlab"` and NO `optional = true`. Every workspace requires the
  in-boundary GitLab login before the agent reports ready, and the agent git
  credential helper injects a short-lived OAuth token for clone/fetch/push to
  `gitlab.<BASE_DOMAIN>`. No PATs or SSH keys live in the workspace.
- Parameters: `cpu` (2/4/8, default 4), `memory` (4/8/16 GB, default 8),
  `disk_size` (10/20/50 GB, default 20, immutable), and `git_repo` (optional
  repo to clone on start).
- Agent: PATH normalization plus a tolerant toolchain step,
  `CODER_AGENT_DEVCONTAINERS_ENABLE=false` (no docker socket in the pod),
  CPU/memory/disk metadata, and `display_apps` for VS Code Desktop, web
  terminal, SSH helper, and port-forwarding helper.
- Browser IDE: `code-server` 1.3.1, `subdomain = true`, folder `/home/coder`.
- Optional `git-clone` 1.0.22 module gated on the `git_repo` parameter.
- Compute: one `kubernetes_pod_v1` and one `kubernetes_persistent_volume_claim_v1`
  in `coder-workspaces`. Pod and container run as uid 1000, `fs_group` 1000.
  Home PVC is `ReadWriteOnce`, sized from `disk_size`, lands on the default
  `gp3` StorageClass at `/home/coder`. Soft pod anti-affinity by hostname.
  Both pod and PVC use `lifecycle { ignore_changes = all }` so a running pod
  survives template re-applies.

## Images (ECR mirror only)

Registry `<ACCOUNT_ID>.dkr.ecr.us-gov-west-1.amazonaws.com`. The only workspace
base mirrored today is:

```
<ACCOUNT_ID>.dkr.ecr.us-gov-west-1.amazonaws.com/docker-hub/codercom/enterprise-base:ubuntu-noble-20260601
```

(upstream `docker.io/codercom/enterprise-base:ubuntu-noble-20260601`,
already in `scripts/images.txt`). Every template defaults to it so the family
builds today with no new mirror work. `enterprise-base` is Ubuntu noble, runs
as user `coder` (uid 1000), and grants passwordless sudo, so the startup
scripts provision language toolchains with `sudo apt-get` (tolerant: each
install is best-effort and never fails the build).

Production-grade, fully air-gapped images (toolchains pre-baked, no startup
apt) are the recommended end state. Candidate richer bases and custom images
that root could mirror or build (via the GitLab CI Kaniko path in
`docs/as-built/70-workspace-templates.md`) are listed as TODOs in
`docs/swarm/handoffs/WS-25-handoff.md`. Do NOT default a template to an image
that is not yet in the ECR mirror.

## Routing by description

Coder Agents auto-selects a template by its `description`, so each template
ships a specific, tool-named description in its `metadata.json` and the root
operator applies it with `coder templates edit <name> --description "..."`
after push (Coder `templates push` does not read `metadata.json`; the orchestrator
applies display name, icon, and description post-push).

## Conventions

- No emdash, endash, or spaced double-hyphen punctuation in any file.
- Partition-safe: nothing here hardcodes a commercial ARN.
- Files end with a newline.
