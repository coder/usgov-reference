# Firewalled Claude Code on Coder Agents (GovCloud demo template)

Coder workspace template that runs **Claude Code as a Coder Agent** inside a
Kubernetes pod on the EKS cluster, wired through the **Coder AI Gateway (AI
Bridge)** and wrapped in the **Coder Boundary agent firewall**. The workspace
never holds a raw Anthropic API key: every request is
proxied through Coder using the workspace owner's session token and routed to
the configured provider (Anthropic-direct primary, Bedrock secondary)
in-boundary.

This is the `claude-code` template with the agent firewall turned on. Claude
Code runs inside a process-level network egress jail (`landjail`, Landlock
LSM) that denies all HTTP(S) egress except an allowlist. The agent can reach
the in-boundary AI Gateway and the in-cluster GitLab; every other destination
is denied and audit-logged. This is the data-exfiltration / DLP guardrail
story for the AOI.

Launching the template as a **Coder Task** opens the Claude Code chat UI and
seeds the agent with the task prompt.

- `main.tf`: the template (providers `coder` + `kubernetes`).
- Workspace image: `codercom/enterprise-base:ubuntu-noble-20260601`, pulled
  from the ECR mirror.

## Agent firewall (Coder Boundary)

The `module "claude_code"` block sets `enable_boundary = true` and
`use_boundary_directly = true`, so the module installs the standalone
`boundary` binary and launches `boundary -- claude`. The allowlist and jail
type come from `~/.config/coder_boundary/config.yaml`, rendered from
`boundary.config.yaml.tftpl` and written by the module `pre_install_script`
before Claude Code starts. The agent env vars `BOUNDARY_CONFIG` and
`BOUNDARY_JAIL_TYPE=landjail` make boundary load that config and use landjail
reliably (boundary v0.9.0 dropped config auto-discovery).

The allowlist (`boundary.config.yaml.tftpl`) is adapted from the Red Hat
Summit 2026 demo (`coder/demo-aigov-rhaiis-rhsummit-2026`). It uses Claude
Code's default allowed domains (most package managers, GitHub, container
registries, cloud SDKs) plus this deployment's Coder host (`${coder_host}`,
rendered from the access URL) and the in-cluster GitLab. **npm is
intentionally omitted**, so asking the agent to `npm install <anything>` is
the obvious DENY in the demo. Edit the `.tftpl` file to change the allowlist;
do not inline rules in `main.tf`.

Why `use_boundary_directly = true`: the default `coder boundary` subcommand
verifies the deployment license via an authenticated client, but the agent
carries only an agent token (no user session), so the subcommand errors with
"not logged in". The standalone binary (MIT) has no license/login dependency.
landjail needs no added pod capabilities; the AL2023 node kernel (6.18) is
well past the Landlock 6.7 floor and `landlock` is in the node LSM stack.

### Verify allow vs deny in a workspace terminal

```bash
# Allowed: the AI Gateway host returns 200
boundary -- curl -sS -o /dev/null -w '%{http_code}\n' \
  https://dev.<BASE_DOMAIN>/api/v2/buildinfo

# Allowed: PyPI is on the allowlist
boundary -- curl -sS -o /dev/null -w '%{http_code}\n' https://pypi.org

# Denied: npm is intentionally off the allowlist (boundary returns 403)
boundary -- curl -sS -o /dev/null -w '%{http_code}\n' https://registry.npmjs.org
```

The headline demo: ask Claude Code to `npm install left-pad`. boundary denies
the egress and the deny shows up live in the boundary Grafana dashboard and
the coderd audit log with owner / workspace / agent attribution. Claude Code
itself keeps working because its `ANTHROPIC_BASE_URL` points at the
allowlisted gateway host. To roll back to an un-firewalled workspace, use the
`claude-code` template instead (or set `enable_boundary = false`).

## What's inside

| Piece | Resource | Notes |
|---|---|---|
| Agent | `coder_agent.main` | startup script, metadata, `display_apps` (VS Code Desktop, web terminal, SSH) |
| Claude Code | `module.claude_code` (`registry.coder.com/coder/claude-code/coder` **4.7.3**) | `enable_aibridge = true`, bundles AgentAPI + Claude Code web app, outputs `task_app_id` |
| Coder Task | `coder_ai_task.claude_code` | binds the Task UI to the Claude Code app; only created in a Task context |
| Browser IDE | `module.code_server` (`code-server` 1.3.1) | extra `coder_app` tile |
| Compute | `kubernetes_pod_v1.workspace` + `kubernetes_persistent_volume_claim_v1.home` | sizing from `cpu` / `memory` / `disk_size` parameters |
| AI auth | `coder_env.anthropic_auth_token` | exports `ANTHROPIC_AUTH_TOKEN` = session token |

Parameters: `cpu`, `memory`, `disk_size`, and `ai_prompt` (fallback prompt for
non-Task builds).

## AI Gateway wiring (end to end)

1. The `claude_code` module is configured with `enable_aibridge = true`. On the
   agent it sets:
   - `ANTHROPIC_BASE_URL = <access_url>/api/v2/aibridge/anthropic`
   - `CLAUDE_API_KEY = <workspace owner session token>`

   With `CODER_ACCESS_URL=https://dev.<BASE_DOMAIN>` the base URL resolves
   to `https://dev.<BASE_DOMAIN>/api/v2/aibridge/anthropic`.
2. This template additionally exports `ANTHROPIC_AUTH_TOKEN` (the same session
   token) to match the AI Gateway client contract in `deploy/CONVENTIONS.md`.
3. Claude Code calls `ANTHROPIC_BASE_URL`. The Coder AI Gateway authenticates
   the session token, applies governance/audit, and forwards the request to the
   active provider:
   - **Anthropic-direct** (primary): egress via the NAT gateway.
   - **Bedrock** (secondary): IRSA on the `coder/coder` service account, model
     `us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0`, in-region only.

No Anthropic key is stored in the workspace; the session token is the only
credential and it is scoped to the workspace owner.

### Model selection

Model is left at the module default on purpose, because the requested model
name must match whichever provider the Gateway has live:

- Anthropic-direct: an Anthropic id, e.g. `claude-sonnet-4-5-20250929`.
- Bedrock (GovCloud): the inference profile
  `us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0`.

Pin one by uncommenting `model = "..."` in the module block once the live
provider is confirmed. Bedrock Claude access was still gated at authoring time
(see `STATUS.md`), so the safe default is to let Claude Code/Gateway negotiate.

### Why module 4.7.3 and `enable_aibridge` (not `enable_ai_gateway`)

Verified against the Coder registry:

- `deploy/CONVENTIONS.md` and `versions.lock.yaml` pin the claude-code module
  to **4.7.3**.
- In **4.7.x the input is `enable_aibridge`**. The `enable_ai_gateway` rename
  (and an `ANTHROPIC_AUTH_TOKEN` the module sets itself) only appear in the
  **5.x** line.
- The 5.x refactor **removed** the bundled AgentAPI integration and the
  `task_app_id` output, which `coder_ai_task` requires. Staying on 4.7.3 is what
  makes the Coder Tasks wiring in this template work.

If the project later moves to claude-code 5.x, switch `enable_aibridge` →
`enable_ai_gateway`, drop the explicit `coder_env.anthropic_auth_token`, and add
a standalone `agentapi` module to supply `task_app_id` for `coder_ai_task`.

## Cluster prerequisites

The platform layer (Coder server + ingress + namespaces) is out of scope for
this directory. Before pushing/using the template, ensure:

1. **Coder server** 2.34.0 with the AI Governance add-on license and the AI
   Gateway providers configured (Anthropic-direct + Bedrock). See
   `deploy/coder/`.
2. **Wildcard access URL** set so subdomain apps work
   (`CODER_WILDCARD_ACCESS_URL=*.<BASE_DOMAIN>`). The Claude Code web app
   and code-server use `subdomain = true`.
3. **Workspaces namespace** exists:

   ```bash
   kubectl create namespace coder-workspaces
   ```

4. **Provisioner RBAC**: the Coder provisioner (service account `coder` in the
   `coder` namespace) must be able to manage pods/PVCs in `coder-workspaces`.
   Example (apply with the platform layer, not from this directory):

   ```yaml
   apiVersion: rbac.authorization.k8s.io/v1
   kind: Role
   metadata:
     name: coder-workspace-provisioner
     namespace: coder-workspaces
   rules:
     - apiGroups: [""]
       resources: ["pods", "persistentvolumeclaims"]
       verbs: ["create", "get", "list", "watch", "update", "patch", "delete"]
     - apiGroups: [""]
       resources: ["pods/exec", "pods/log"]
       verbs: ["get", "create"]
     - apiGroups: [""]
       resources: ["events"]
       verbs: ["get", "list", "watch"]
   ---
   apiVersion: rbac.authorization.k8s.io/v1
   kind: RoleBinding
   metadata:
     name: coder-workspace-provisioner
     namespace: coder-workspaces
   roleRef:
     apiGroup: rbac.authorization.k8s.io
     kind: Role
     name: coder-workspace-provisioner
   subjects:
     - kind: ServiceAccount
       name: coder
       namespace: coder
   ```

5. **Image pull**: the EKS node IAM role needs ECR read
   (`ecr:GetAuthorizationToken`, `ecr:BatchGetImage`,
   `ecr:GetDownloadUrlForLayer`) for
   `<ACCOUNT_ID>.dkr.ecr.us-gov-west-1.amazonaws.com`. With that on the node
   role, no `imagePullSecret` is required on the pod. The image must already be
   mirrored into ECR (`scripts/mirror-images.sh`).

## Pushing the template

From the repo root:

```bash
# First time: create the template.
coder templates push claude-code \
  --directory coder-templates/claude-code \
  --variable namespace=coder-workspaces

# Subsequent updates push a new version.
coder templates push claude-code \
  --directory coder-templates/claude-code
```

Override the image or namespace at push time if needed:

```bash
coder templates push claude-code \
  --directory coder-templates/claude-code \
  --variable namespace=coder-workspaces \
  --variable workspace_image=<ACCOUNT_ID>.dkr.ecr.us-gov-west-1.amazonaws.com/docker-hub/codercom/enterprise-base:ubuntu-noble-20260601
```

Template variables:

| Variable | Default | Purpose |
|---|---|---|
| `namespace` | `coder-workspaces` | namespace for workspace pods |
| `workspace_image` | ECR-mirrored `enterprise-base` | workspace container image |
| `use_kubeconfig` | `false` | use a host kubeconfig instead of in-cluster config |

## Using it

- **As a workspace**: create a workspace from the template, open VS Code /
  terminal / code-server, and run `claude` in the workspace.
- **As a Task**: create a Coder Task from this template and enter a prompt.
  Coder injects the prompt via `data.coder_task.me.prompt`, the
  `coder_ai_task` resource binds the Task UI to the Claude Code app, and the
  agent reports status back to the Coder UI through AgentAPI.

## Verification status

| Item | Source | Status |
|---|---|---|
| claude-code 4.7.3 inputs (`enable_aibridge`, `workdir`, `ai_prompt`, `report_tasks`, `subdomain`) and `task_app_id` output | module `main.tf` / `README.md` at tag `release/coder/claude-code/v4.7.3` | verified |
| `coder_ai_task.app_id` + `data.coder_task` (`enabled`, `prompt`) | `coder/terraform-provider-coder` docs; first shipped in provider **v2.13.0** | verified |
| Workspace image tag | Docker Hub `codercom/enterprise-base` | verified (`ubuntu-noble-20260601`) |
| `code-server` 1.3.1 | registry tag `release/coder/code-server/v1.3.1` | verified (latest is 1.5.0) |
| Live AI Gateway routing / Bedrock model access | runtime cluster | NOT verified here (no live infra access; Bedrock Claude access gated per `STATUS.md`) |
