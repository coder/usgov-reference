# =============================================================================
# Generic agent runtime workspace (GovCloud demo, EKS, plain compute, hardened)
# =============================================================================
# A general-purpose, language-agnostic workspace that runs in a Kubernetes pod
# on the EKS cluster. It is the default target for the Coder Agents path when
# the task is not language-specific: the AI runs server-side on the control
# plane and operates this workspace remotely (read_file, write_file, execute).
#
# This template is intentionally PLAIN COMPUTE and hardened:
#   - NO AI Gateway wiring, NO LLM keys, NO agent harness, NO LLM CLIs.
#   - Privilege escalation is DISABLED (no_new_privs): the startup script does
#     not use sudo/apt and relies on the prebaked image plus non-privileged
#     user installs into ~/.local/bin.
#   - Egress only needs the in-boundary GitLab and the Coder control plane,
#     never an LLM provider. This makes it a candidate for a tighter egress
#     policy (git plus coderd only), enforced at the platform layer. If that
#     policy is applied, any extra tooling (including the code-server binary)
#     must be prebaked into an in-boundary image rather than fetched at startup.
#
# Shared pattern and invariants: ../_shared/README.md.
# =============================================================================

terraform {
  required_providers {
    coder = {
      source  = "coder/coder"
      version = ">= 2.13.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.23"
    }
  }
}

provider "coder" {}

variable "use_kubeconfig" {
  type        = bool
  description = "Use a host kubeconfig instead of in-cluster config. Leave false when the Coder provisioner runs inside the cluster."
  default     = false
}

variable "namespace" {
  type        = string
  description = "Kubernetes namespace that hosts workspace pods."
  default     = "coder-workspaces"
}

# Workspace container image (ECR mirror).
#
# Upstream ref : docker.io/codercom/enterprise-base:ubuntu-noble-20260601
# ECR mirror   : <registry>/docker-hub/codercom/enterprise-base:ubuntu-noble-20260601
#
# enterprise-base is Ubuntu noble, runs as user `coder` (uid 1000), and ships
# git/curl. Because this template disables privilege escalation, it does NOT
# apt-install at startup; whatever the server-side agent needs beyond git and a
# shell should be prebaked into the image. enterprise-base is the only base
# mirrored today. A richer prebaked generic image (git, build-essential,
# Python, Node, ripgrep, jq) is the recommended end state; see the WS-25
# handoff for the root mirror/build TODO.
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

# -----------------------------------------------------------------------------
# Required GitLab external auth (in-boundary)
# -----------------------------------------------------------------------------
# id MUST match CODER_EXTERNAL_AUTH_0_ID on the Coder server ("gitlab").
# Declaring this without optional=true makes the workspace REQUIRE a GitLab
# login: the dashboard surfaces a "Login with GitLab" control and the agent
# only reports ready once the owner completes the in-boundary OAuth flow.
data "coder_external_auth" "gitlab" {
  id = "gitlab"
}

# -----------------------------------------------------------------------------
# Parameters
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

data "coder_parameter" "git_repo" {
  name         = "git_repo"
  display_name = "Git Repository"
  description  = "Optional GitLab repository to clone into /home/coder on start. Uses the workspace GitLab login."
  type         = "string"
  default      = ""
  mutable      = false
  icon         = "/icon/git.svg"
}

# -----------------------------------------------------------------------------
# Agent
# -----------------------------------------------------------------------------

resource "coder_agent" "main" {
  arch = data.coder_provisioner.me.arch
  os   = "linux"

  # Minimal, hardened startup: PATH normalization only. No sudo/apt, because
  # privilege escalation is disabled on the container. Any richer tooling for
  # the server-side agent should be prebaked into the workspace image (see the
  # WS-25 handoff). The line stays tolerant so it never fails the build.
  startup_script = <<-EOT
    #!/bin/bash
    set +e
    touch ~/.bashrc
    mkdir -p ~/.local/bin
    grep -qF '$HOME/.local/bin' ~/.profile 2>/dev/null || \
      echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.profile

    echo "=== Workspace ready ==="
  EOT

  env = {
    EDITOR = "code"
    VISUAL = "code"

    # No docker socket in the pod; opt out of devcontainer auto-detection so
    # the dashboard does not hang polling `docker ps`.
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
# Registry modules
# -----------------------------------------------------------------------------

# code-server: VS Code in the browser on a subdomain. Installs to ~/.local
# without sudo, so it works with privilege escalation disabled. If a git-only
# egress policy is applied, prebake the code-server binary into the image and
# pin the module to an offline install.
module "code_server" {
  count     = data.coder_workspace.me.start_count
  source    = "registry.coder.com/coder/code-server/coder"
  version   = "1.3.1"
  agent_id  = coder_agent.main.id
  folder    = "/home/coder"
  subdomain = true
  order     = 1
}

# git-clone: clone the assigned repo on start (uses the GitLab login above).
module "git_clone" {
  count    = data.coder_parameter.git_repo.value != "" ? data.coder_workspace.me.start_count : 0
  source   = "registry.coder.com/coder/git-clone/coder"
  version  = "1.0.22"
  agent_id = coder_agent.main.id
  url      = data.coder_parameter.git_repo.value
  base_dir = "/home/coder"
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
        # Hardened posture: this template installs nothing via sudo, so privilege
        # escalation is disabled. This sets no_new_privs on the container.
        allow_privilege_escalation = false
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

  lifecycle {
    ignore_changes = all
  }
}
