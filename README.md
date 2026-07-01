# usgov-reference

A public, forkable reference deployment of [Coder](https://coder.com) on
AWS GovCloud EKS, intended for teams that want to stand up their own
GovCloud Coder environment based on proven, battle-tested patterns.

> **Status: v0. Structural scaffold only.**
> The CI gates, placeholder schema, and sanitized documentation are in place.
> Full parameterization of the `deploy/`, `gitops/`, `terraform/`, and
> `coder-templates/` trees is in progress and will land incrementally.
> See [Pending items](#pending-items) below.

---

## What this repository is

This repo captures the *patterns and structure* for running Coder in a
US-Government AWS GovCloud account. It is the public half of a
private/public split:

| Repo | Audience | Contains |
|---|---|---|
| **usgov-reference** (this repo) | Public (fork and adapt) | Sanitized patterns, placeholder schema, CI gates |
| Private live-env repo | Internal operators | Real identifiers, live state, operator runbooks |

No real account IDs, domain names, certificate ARNs, VPC/subnet/SG IDs,
or other environment-specific identifiers appear in this repository.
Enforcement is two-layer:

1. **Generic regex scanner** (`scripts/check-identifiers.sh`, runs in CI):
   flags 12-digit account IDs in ARN/ECR contexts, AWS resource IDs,
   ACM certificate UUIDs, and an optional configurable base domain.
2. **Literal denylist** (private, in the private upstream repository): lists
   the specific live-env identifiers and runs in the promotion pipeline before
   any file is published here. Real values never enter this public tree.

---

## Architecture summary

- **EKS control plane** (3 AZs, private subnets) running Coder, Keycloak,
  Istio, Grafana/Prometheus/Loki, and the External Secrets Operator.
- **RDS PostgreSQL** Multi-AZ for Coder and Keycloak state.
- **ECR** private registry (mirrored from public upstreams; no pull-through
  in GovCloud).
- **GitLab Omnibus** on EC2 for in-boundary source control.
- **AWS Bedrock** (IRSA, no static keys) for AI Gateway workloads.
- **Route53** (GovCloud hosted zone, NS-delegated from a commercial account).
- **Terraform** IaC for VPC, EKS, RDS, IAM/IRSA, and ECR.
- **Flux GitOps** for Helm release management.

See [`docs/architecture-overview.md`](docs/architecture-overview.md) for
the full narrative.

---

## Prerequisites

Install these tools before working with this repository:

| Tool | Minimum version | Notes |
|---|---|---|
| [aws CLI](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) | 2.x | GovCloud profile support requires v2 |
| [terraform](https://developer.hashicorp.com/terraform/install) | 1.9 | Matches provider constraints in `terraform/versions.tf` |
| [helm](https://helm.sh/docs/intro/install/) | 3.x | Used for Helm chart rendering and dry-run checks |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | 1.28 (client) | Match the `<KUBERNETES_VERSION>` you deploy |
| [gitleaks](https://github.com/gitleaks/gitleaks#installing) | 8.x | Secret pattern scanner; also runs in CI |
| [yamllint](https://yamllint.readthedocs.io/) | 1.35 | YAML static analysis; also runs in CI |
| [tflint](https://github.com/terraform-linters/tflint#installation) | 0.52 | Terraform linter |
| [shellcheck](https://www.shellcheck.net/) | 0.9 | Shell script linter |

---

## Repository layout

```
.
├── docs/                          # Sanitized architecture and runbook docs
│   └── architecture-overview.md
├── scripts/
│   ├── check-identifiers.sh       # Generic identifier pattern scanner
│   └── forbidden-strings.txt      # Identifier format reference (no real values)
├── values/
│   └── env.example.yaml           # Full placeholder schema for a new environment
├── .github/workflows/
│   └── ci.yml                     # Gitleaks + identifier scan + yamllint CI gates
├── CONTRIBUTING.md
└── README.md
```

### Pending items (deferred to follow-up PRs)

The following source trees from the private live-env repo will be
parameterized and added here incrementally. Each item requires replacing
real identifiers with `<PLACEHOLDER>` tokens that callers supply via
`values/env.example.yaml` (or a fork of it):

| Tree | Notes |
|---|---|
| `terraform/` | VPC, EKS, RDS, IRSA, ECR modules; all account/zone/cert IDs parameterized |
| `deploy/platform/` | ingress-nginx, AWS LBC, node pools, namespaces |
| `deploy/coder/` | Helm values, External Secrets objects |
| `deploy/keycloak/` | Helm values, realm JSON |
| `deploy/istio/` | Operator config, VirtualServices, DestinationRules (no live cert ARN) |
| `deploy/observability/` | Grafana/Prom/Loki stack, datasources (no live RDS endpoint) |
| `deploy/gitlab/` | Omnibus config, runner registration |
| `gitops/` | Flux HelmRelease/Kustomization manifests |
| `coder-templates/` | Workspace templates (EKS, OCP, AI agent variants) |
| `scripts/` | Bootstrap, mirror-images, preflight, persona setup |

The following support files are referenced in the schema but will be added
by the base parameterization follow-up:

- `versions.lock.yaml`: chart version lock file referenced in `values/env.example.yaml`.
- `deploy/coder/secrets.example.yaml`: full placeholder schema for Coder secrets;
  the current scaffold is incomplete pending parameterization.

---

## Quick start (fork and adapt)

1. **Fork** this repository.
2. **Copy** `values/env.example.yaml` to a file outside the repo (e.g.,
   `~/.config/myenv/env.yaml`) and fill in every `<PLACEHOLDER>`.
3. **Bootstrap** state backend (S3 bucket + DynamoDB table) before running
   Terraform:
   ```bash
   aws s3 mb s3://<CLUSTER_NAME>-tfstate-<ACCOUNT_ID> --region <REGION>
   aws dynamodb create-table \
     --table-name <CLUSTER_NAME>-tflock \
     --attribute-definitions AttributeName=LockID,AttributeType=S \
     --key-schema AttributeName=LockID,KeyType=HASH \
     --billing-mode PAY_PER_REQUEST \
     --region <REGION>
   ```
4. **Apply Terraform** to provision VPC, EKS, RDS, IAM, and ECR.
5. **Mirror images** into your ECR registry (see `scripts/mirror-images.sh`
   once it is promoted).
6. **Install the platform layer** (ingress-nginx, LBC, ESO, Istio) via Flux
   or direct Helm.
7. **Install Coder** via the official Helm chart (see `deploy/coder/` once
   promoted).
8. **Run the CI gate** locally before any push:
   ```bash
   bash scripts/check-identifiers.sh
   ```

---

## CI gates

| Job | Tool | What it checks |
|---|---|---|
| `gitleaks` | [gitleaks](https://github.com/gitleaks/gitleaks) | Generic secret patterns (API keys, tokens, credentials) |
| `check-identifiers` | `scripts/check-identifiers.sh` | AWS account IDs in ARN/ECR, resource IDs, ACM cert UUIDs, optional base domain |
| `yamllint` | [yamllint](https://yamllint.readthedocs.io/) | YAML syntax and style across all `.yaml` and `.yml` files |

All jobs run on every pull request and push to `main`.
A PR may not be merged if any job fails.

---

## License

See [LICENSE](LICENSE) (to be added).
