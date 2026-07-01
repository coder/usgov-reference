# =============================================================================
# Firewalled Claude Code on Coder Agents, GovCloud demo workspace template
# =============================================================================
# Identical to the claude-code template, with the Coder Boundary agent
# firewall enabled. Claude Code runs inside a process-level network egress
# jail (landjail / Landlock LSM) that enforces an HTTP(S) allowlist. The
# agent can reach the in-boundary AI Gateway and the in-cluster GitLab, and
# every other egress is denied and audit-logged. This is the data-exfil /
# DLP guardrail story for the AOI.
#
# Boundary wiring (claude-code module 4.7.3 inputs):
#   - enable_boundary       = true     wraps Claude Code with the firewall.
#   - use_boundary_directly = true     installs the standalone boundary
#     binary (MIT) instead of the `coder boundary` subcommand. The subcommand
#     path needs a logged-in coder CLI session (license check); the agent has
#     only an agent token, so the standalone binary is the reliable path.
#   - The module adds no --allow / --jail-type flags, so the allowlist and
#     jail type come from ~/.config/coder_boundary/config.yaml. That file is
#     rendered from boundary.config.yaml.tftpl and written by
#     pre_install_script; BOUNDARY_CONFIG + BOUNDARY_JAIL_TYPE agent env vars
#     make boundary load it and use landjail reliably.
#
# Allowlist (boundary.config.yaml.tftpl): adapted from the Red Hat Summit
# 2026 demo (coder/demo-aigov-rhaiis-rhsummit-2026), which uses Claude
# Code's default allowed domains (package managers, GitHub, container
# registries, cloud SDKs) plus this deployment's Coder host and the
# in-cluster GitLab. npm is intentionally omitted so `npm install` is the
# obvious DENY in the demo. jail_type landjail needs no added capabilities
# (AL2023 kernel 6.18 exceeds the Landlock 6.7 floor; landlock is in the
# node LSM stack).
#
# Runs Claude Code as a Coder Agent inside a Kubernetes pod on the EKS
# cluster. Claude Code is wired through the Coder AI Gateway (AI Bridge)
# so the workspace never holds a raw Anthropic key: requests are proxied
# through Coder using the workspace owner's session token and routed to
# the configured provider (Anthropic-direct primary / Bedrock secondary)
# in-boundary.
#
# Launching this template as a Coder Task surfaces the Claude Code chat UI
# (via the bundled AgentAPI app) and seeds the agent with the task prompt.
#
# VERSION / INPUT NAMING, verified against the Coder registry:
#   - claude-code module is pinned to 4.7.3 (the version in
#     deploy/CONVENTIONS.md / versions.lock.yaml).
#   - In 4.7.3 the AI Gateway input is named `enable_aibridge` (NOT
#     `enable_ai_gateway`). The `enable_ai_gateway` rename landed in the
#     5.x line, which also REMOVED the bundled AgentAPI integration and
#     the `task_app_id` output that `coder_ai_task` depends on. Staying on
#     4.7.3 is what makes the Coder Tasks wiring below possible.
#   - `enable_aibridge = true` makes the module set, on the agent:
#       ANTHROPIC_BASE_URL = <access_url>/api/v2/aibridge/anthropic
#       CLAUDE_API_KEY     = <workspace owner session token>
#     With CODER_ACCESS_URL=https://dev.<BASE_DOMAIN> the base URL
#     resolves to https://dev.<BASE_DOMAIN>/api/v2/aibridge/anthropic.
#   - We additionally export ANTHROPIC_AUTH_TOKEN (session token) to match
#     the AI Gateway client contract in deploy/CONVENTIONS.md.
#
# See README.md for the end-to-end AI Gateway wiring and cluster
# prerequisites (namespace + provisioner RBAC).
# =============================================================================

terraform {
  required_providers {
    coder = {
      source = "coder/coder"
      # `data.coder_task` and `coder_ai_task.app_id` require provider >= 2.13.0.
      version = ">= 2.13.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.23"
    }
  }
}

# -----------------------------------------------------------------------------
# Providers
# -----------------------------------------------------------------------------

provider "coder" {}

variable "use_kubeconfig" {
  type        = bool
  description = "Use a host kubeconfig instead of in-cluster config. Leave false when the Coder provisioner runs inside the cluster."
  default     = false
}

variable "namespace" {
  type        = string
  description = "Kubernetes namespace that hosts workspace pods. The platform layer must create this namespace and grant the provisioner RBAC (see README)."
  default     = "coder-workspaces"
}

# Workspace container image (ECR mirror).
#
# Upstream ref : docker.io/codercom/enterprise-base:ubuntu-noble-20260601
# ECR mirror   : per deploy/CONVENTIONS.md the docker.io -> ECR mapping is
#                docker.io/<repo>:<tag> -> <registry>/docker-hub/<repo>:<tag>
#
# codercom/enterprise-base is Coder's maintained Kubernetes workspace base
# image: runs as user `coder` (uid 1000), ships git/curl/sudo, and is the
# canonical base for Coder's official Kubernetes template. Claude Code and
# AgentAPI install as standalone binaries into $HOME/.local/bin, so no
# Node.js/npm is required in the base image.
variable "workspace_image" {
  type        = string
  description = "Fully-qualified workspace image. Defaults to the ECR-mirrored codercom/enterprise-base."
  default     = "<ACCOUNT_ID>.dkr.ecr.us-gov-west-1.amazonaws.com/docker-hub/codercom/enterprise-base:ubuntu-noble-20260601"
}

provider "kubernetes" {
  config_path = var.use_kubeconfig ? "~/.kube/config" : null
}

data "coder_provisioner" "me" {}
data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

# Populated when the workspace is created as a Coder Task. `enabled` is
# false for a normal workspace build, and `prompt` carries the task prompt.
data "coder_task" "me" {}

# -----------------------------------------------------------------------------
# Git external auth: in-cluster GitLab (in-boundary)
# -----------------------------------------------------------------------------
# Every workspace authenticates git against the in-cluster GitLab through
# Coder's external-auth provider `gitlab` (configured on the Coder server, see
# deploy/coder/values.yaml CODER_EXTERNAL_AUTH_0_*). Declaring this data source
# makes the workspace REQUIRE a GitLab login: the dashboard surfaces a "Login
# with GitLab" control and the agent only reports the auth as satisfied once
# the owner has completed the OAuth flow. The Coder agent's git credential
# helper then injects the short-lived OAuth token for any clone/fetch/push to
# gitlab.<BASE_DOMAIN>. No PATs or SSH keys live in the workspace, and no
# auth path leaves the GovCloud boundary.
#
# id MUST match CODER_EXTERNAL_AUTH_0_ID on the Coder server ("gitlab").
data "coder_external_auth" "gitlab" {
  id = "gitlab"
}

# -----------------------------------------------------------------------------
# Parameters: sizing and the AI task prompt
# -----------------------------------------------------------------------------

data "coder_parameter" "cpu" {
  name         = "cpu"
  display_name = "CPU Cores"
  description  = "CPU limit for the workspace pod."
  type         = "number"
  default      = "4"
  mutable      = true
  icon         = "/icon/memory.svg"

  option {
    name  = "2 Cores"
    value = "2"
  }
  option {
    name  = "4 Cores"
    value = "4"
  }
  option {
    name  = "8 Cores"
    value = "8"
  }
}

data "coder_parameter" "memory" {
  name         = "memory"
  display_name = "Memory (GB)"
  description  = "Memory limit for the workspace pod."
  type         = "number"
  default      = "8"
  mutable      = true
  icon         = "/icon/memory.svg"

  option {
    name  = "4 GB"
    value = "4"
  }
  option {
    name  = "8 GB"
    value = "8"
  }
  option {
    name  = "16 GB"
    value = "16"
  }
}

data "coder_parameter" "disk_size" {
  name         = "disk_size"
  display_name = "Disk Size (GB)"
  description  = "Persistent /home/coder volume size. Cannot be changed after creation."
  type         = "number"
  default      = "20"
  mutable      = false
  icon         = "/icon/database.svg"

  option {
    name  = "10 GB"
    value = "10"
  }
  option {
    name  = "20 GB"
    value = "20"
  }
  option {
    name  = "50 GB"
    value = "50"
  }
}

# Fallback prompt for non-Task workspace builds. When the workspace is
# launched as a Coder Task, data.coder_task.me.prompt takes precedence.
data "coder_parameter" "ai_prompt" {
  name         = "ai_prompt"
  display_name = "Initial AI Prompt"
  description  = "Seed prompt for Claude Code. Ignored when launched as a Coder Task (the Task prompt is used instead)."
  type         = "string"
  default      = ""
  mutable      = true
  icon         = "/icon/claude.svg"
}

locals {
  # Prefer the Coder Task prompt; fall back to the parameter for plain builds.
  effective_prompt = data.coder_task.me.prompt != "" ? data.coder_task.me.prompt : data.coder_parameter.ai_prompt.value

  # For documentation/readme parity. The claude-code module derives the
  # same value internally from data.coder_workspace.me.access_url.
  ai_gateway_anthropic_url = "${data.coder_workspace.me.access_url}/api/v2/aibridge/anthropic"

  # Coder access URL host, substituted into the boundary allowlist so the
  # agent can reach the AI Gateway, AgentAPI, and the workspace agent.
  coder_host = replace(replace(data.coder_workspace.me.access_url, "https://", ""), "http://", "")

  # Agent firewall allowlist, rendered from the sibling
  # boundary.config.yaml.tftpl (adapted from the Red Hat Summit 2026 demo).
  # Edit that file to change the allowlist; do not inline rules here.
  boundary_config_yaml = templatefile("${path.module}/boundary.config.yaml.tftpl", {
    coder_host = local.coder_host
  })
}

# -----------------------------------------------------------------------------
# Agent
# -----------------------------------------------------------------------------

resource "coder_agent" "main" {
  arch = data.coder_provisioner.me.arch
  os   = "linux"

  # Claude Code + AgentAPI are installed by the claude-code module's own
  # coder_script (native binaries into $HOME/.local/bin). This startup
  # script only normalizes PATH and signals readiness.
  startup_script = <<-EOT
    #!/bin/bash
    set -e
    touch ~/.bashrc
    grep -qF '$HOME/.local/bin' ~/.profile 2>/dev/null || \
      echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.profile
    echo "=== Workspace ready ==="
  EOT

  env = {
    EDITOR = "code"
    VISUAL = "code"

    # No docker socket in the pod; opt out of devcontainer auto-detection
    # so the dashboard does not hang polling `docker ps`.
    CODER_AGENT_DEVCONTAINERS_ENABLE = "false"
  }

  metadata {
    display_name = "CPU Usage"
    key          = "cpu_usage"
    script       = "coder stat cpu"
    interval     = 10
    timeout      = 1
  }

  metadata {
    display_name = "Memory Usage"
    key          = "mem_usage"
    script       = "coder stat mem"
    interval     = 10
    timeout      = 1
  }

  metadata {
    display_name = "Disk Usage"
    key          = "disk_usage"
    script       = "coder stat disk --path /home/coder"
    interval     = 60
    timeout      = 1
  }

  display_apps {
    vscode                 = true
    vscode_insiders        = false
    web_terminal           = true
    ssh_helper             = true
    port_forwarding_helper = true
  }
}

# -----------------------------------------------------------------------------
# AI Gateway client auth
# -----------------------------------------------------------------------------
# The claude-code module (enable_aibridge = true) already sets
# ANTHROPIC_BASE_URL and CLAUDE_API_KEY. We additionally export
# ANTHROPIC_AUTH_TOKEN with the workspace owner's session token to match
# the AI Gateway client contract documented in deploy/CONVENTIONS.md. Both
# carry the same session token, so there is no conflict; no raw Anthropic
# API key is ever placed in the workspace.
resource "coder_env" "anthropic_auth_token" {
  agent_id = coder_agent.main.id
  name     = "ANTHROPIC_AUTH_TOKEN"
  value    = data.coder_workspace_owner.me.session_token
}

# -----------------------------------------------------------------------------
# Agent firewall env
# -----------------------------------------------------------------------------
# boundary v0.9.0 no longer auto-discovers ~/.config/coder_boundary/config.yaml,
# so point it at the rendered config explicitly and pin landjail. These env
# vars are read by both the module-launched `boundary -- claude` and any
# manual `boundary -- <cmd>` run in a workspace terminal.
resource "coder_env" "boundary_config" {
  agent_id = coder_agent.main.id
  name     = "BOUNDARY_CONFIG"
  value    = "/home/coder/.config/coder_boundary/config.yaml"
}

resource "coder_env" "boundary_jail_type" {
  agent_id = coder_agent.main.id
  name     = "BOUNDARY_JAIL_TYPE"
  value    = "landjail"
}

# -----------------------------------------------------------------------------
# Claude Code (Coder registry module) + Coder Task
# -----------------------------------------------------------------------------

module "claude_code" {
  source   = "registry.coder.com/coder/claude-code/coder"
  version  = "4.7.3"
  agent_id = coder_agent.main.id

  # Required by the module: directory Claude Code runs in. Pre-created and
  # trust-accepted by the module.
  workdir = "/home/coder"

  # Route Claude Code through the Coder AI Gateway (AI Bridge) instead of
  # talking to api.anthropic.com directly. Sets ANTHROPIC_BASE_URL +
  # CLAUDE_API_KEY (session token) on the agent. Mutually exclusive with
  # claude_api_key / claude_code_oauth_token.
  enable_aibridge = true

  # ---------------------------------------------------------------------------
  # Coder Boundary agent firewall (this is the "firewalled" variant)
  # ---------------------------------------------------------------------------
  # Wrap Claude Code in a process-level network egress jail. The module
  # launches boundary as a wrapper around the claude process, denying all
  # egress except the allowlist below. landjail uses the Landlock LSM and
  # needs no added pod capabilities.
  enable_boundary = true

  # Install the standalone boundary binary (MIT) rather than using the
  # `coder boundary` subcommand. The subcommand verifies the deployment
  # license via an authenticated client; the agent only carries an agent
  # token (no user session), so the subcommand path errors with "not logged
  # in". The standalone binary has no license/login dependency.
  use_boundary_directly = true
  boundary_version      = "latest"

  # The 4.7.3 module passes no --allow / --jail-type flags to boundary, so
  # the allowlist and jail type come ONLY from
  # ~/.config/coder_boundary/config.yaml. That file is rendered from the
  # sibling boundary.config.yaml.tftpl (Red Hat Summit 2026 allowlist) and
  # written here, before Claude Code starts. The BOUNDARY_CONFIG and
  # BOUNDARY_JAIL_TYPE agent env vars (below) make boundary load it reliably
  # and use landjail even though boundary v0.9.0 dropped config
  # auto-discovery. The base64 round-trip keeps the multi-line YAML intact
  # inside the heredoc. Allowing the Coder host is REQUIRED: it is the AI
  # Gateway egress Claude Code depends on. Everything not listed is denied
  # and audited (npm is intentionally omitted as the demo DENY).
  pre_install_script = <<-EOT
    #!/bin/bash
    set -e
    mkdir -p "$HOME/.config/coder_boundary" /tmp/boundary_logs
    cfg="$HOME/.config/coder_boundary/config.yaml"
    echo '${base64encode(local.boundary_config_yaml)}' | base64 -d > "$cfg"
    chmod 600 "$cfg"
    echo "[firewalled] wrote boundary config ($(grep -c '^  - ' "$cfg") allow rules)"
  EOT

  # Coder Tasks: seed the agent and report task status to the Coder UI via
  # AgentAPI. Empty string for plain builds -> Claude Code starts idle.
  ai_prompt    = local.effective_prompt
  report_tasks = true

  # Serve the Claude Code web app on a subdomain. Requires the wildcard
  # access URL (*.<BASE_DOMAIN>) configured on the Coder server.
  subdomain = true

  # Model selection is intentionally left at the module default. With the
  # AI Gateway, the requested model name must match the active provider:
  #   - Anthropic-direct (primary): an Anthropic model id, e.g.
  #     "claude-sonnet-4-5-20250929".
  #   - Bedrock (secondary): the GovCloud inference profile, e.g.
  #     "us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0".
  # Pin one explicitly only after confirming which provider is live:
  # model = "claude-sonnet-4-5-20250929"
}

# Marks this workspace build as a Coder AI Task and binds the Task UI to the
# Claude Code AgentAPI app. Only created in a Task context so normal
# workspace builds are unaffected.
resource "coder_ai_task" "claude_code" {
  count  = data.coder_task.me.enabled ? data.coder_workspace.me.start_count : 0
  app_id = module.claude_code.task_app_id
}

# code-server: VS Code in the browser (an additional coder_app tile).
module "code_server" {
  count     = data.coder_workspace.me.start_count
  source    = "registry.coder.com/coder/code-server/coder"
  version   = "1.3.1"
  agent_id  = coder_agent.main.id
  folder    = "/home/coder"
  subdomain = true
  order     = 1
}

# -----------------------------------------------------------------------------
# Kubernetes resources
# -----------------------------------------------------------------------------

resource "kubernetes_persistent_volume_claim_v1" "home" {
  metadata {
    name      = "coder-${data.coder_workspace.me.id}-home"
    namespace = var.namespace
    labels = {
      "app.kubernetes.io/name"     = "coder-workspace"
      "app.kubernetes.io/instance" = "coder-${data.coder_workspace.me.id}"
      "app.kubernetes.io/part-of"  = "coder"
    }
  }
  wait_until_bound = false
  spec {
    access_modes = ["ReadWriteOnce"]
    resources {
      requests = {
        storage = "${data.coder_parameter.disk_size.value}Gi"
      }
    }
  }

  lifecycle {
    ignore_changes = all
  }
}

resource "kubernetes_pod_v1" "workspace" {
  count = data.coder_workspace.me.start_count

  metadata {
    name      = "coder-${data.coder_workspace.me.id}"
    namespace = var.namespace
    labels = {
      "app.kubernetes.io/name"     = "coder-workspace"
      "app.kubernetes.io/instance" = "coder-${data.coder_workspace.me.id}"
      "app.kubernetes.io/part-of"  = "coder"
    }
  }

  spec {
    # enterprise-base runs as the `coder` user (uid/gid 1000).
    security_context {
      run_as_user = 1000
      fs_group    = 1000
    }

    container {
      name              = "dev"
      image             = var.workspace_image
      image_pull_policy = "IfNotPresent"
      command           = ["sh", "-c", coder_agent.main.init_script]

      security_context {
        run_as_user = 1000
        # enterprise-base grants the coder user passwordless sudo. The
        # claude-code/agentapi module installs the agentapi binary to
        # /usr/local/bin via sudo, which requires privilege escalation.
        # Disabling it sets the kernel no_new_privs flag and breaks that
        # install (and the Coder Tasks chat UI it powers).
        allow_privilege_escalation = true
      }

      env {
        name  = "CODER_AGENT_TOKEN"
        value = coder_agent.main.token
      }

      env {
        name  = "CODER_AGENT_URL"
        value = data.coder_workspace.me.access_url
      }

      resources {
        requests = {
          "cpu"    = "500m"
          "memory" = "${max(2, floor(data.coder_parameter.memory.value / 2))}Gi"
        }
        limits = {
          "cpu"    = "${data.coder_parameter.cpu.value}"
          "memory" = "${data.coder_parameter.memory.value}Gi"
        }
      }

      volume_mount {
        mount_path = "/home/coder"
        name       = "home"
        read_only  = false
      }
    }

    volume {
      name = "home"
      persistent_volume_claim {
        claim_name = kubernetes_persistent_volume_claim_v1.home.metadata[0].name
      }
    }

    affinity {
      pod_anti_affinity {
        preferred_during_scheduling_ignored_during_execution {
          weight = 1
          pod_affinity_term {
            topology_key = "kubernetes.io/hostname"
            label_selector {
              match_expressions {
                key      = "app.kubernetes.io/name"
                operator = "In"
                values   = ["coder-workspace"]
              }
            }
          }
        }
      }
    }
  }

  # The agent token is baked into init_script; ignore_changes keeps a
  # running pod intact across template re-applies / prebuild claims.
  lifecycle {
    ignore_changes = all
  }
}
