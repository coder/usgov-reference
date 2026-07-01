# App-layer conventions (the contract)

Shared facts for every app-layer workstream. Read this before drafting.
Draft files ONLY in your assigned directory. Do not run terraform, kubectl,
helm, or aws against live infra. Do not edit `terraform/` or other
workstreams' directories. Return a concise report plus the list of container
images your workstream needs (fully-qualified upstream refs, pinned tags).

## Account / region

- Partition `aws-us-gov`, account `<ACCOUNT_ID>`, region `us-gov-west-1`.
- Domain: `<BASE_DOMAIN>`.

## Hostnames (single ACM cert covers `<BASE_DOMAIN>` + `*.<BASE_DOMAIN>`)

| Host | Service |
|---|---|
| `dev.<BASE_DOMAIN>` | Coder dashboard |
| `*.<BASE_DOMAIN>` | Coder workspace apps (wildcard) |
| `auth.<BASE_DOMAIN>` | Keycloak |
| `gitlab.<BASE_DOMAIN>` | GitLab |

ACM cert ARN: `arn:aws-us-gov:acm:us-gov-west-1:<ACCOUNT_ID>:certificate/<ACM_CERT_UUID>`

## Ingress (locked)

One internet-facing **NLB → ingress-nginx → one ACM cert**. TLS terminates at
the NLB via the AWS LB annotations on the ingress-nginx controller Service
(`aws-load-balancer-ssl-cert` = the ACM ARN, `aws-load-balancer-type=external`,
`nlb-target-type=ip`, ssl-ports=443). Backends are plain HTTP. Each app exposes
an `Ingress` with `ingressClassName: nginx` and its host from the table above.
The platform layer (owned by the orchestrator) installs ingress-nginx and the
namespaces; your workstream only declares its own `Ingress` object.

## Namespaces

`coder`, `keycloak`, `gitlab`. Service accounts created per app.

## Versions (source of truth: `versions.lock.yaml`)

- EKS / k8s **1.36**, PostgreSQL **18.4**
- Coder **2.34.1** (Helm chart + `ghcr.io/coder/coder:v2.34.1`)
- Keycloak **26.6.3**
- GitLab CE **19.0.1**
- claude-code Coder module **4.7.3**

## Images (ECR mirror; no pull-through in GovCloud)

Registry: `<ACCOUNT_ID>.dkr.ecr.us-gov-west-1.amazonaws.com`. The orchestrator
populates `scripts/images.txt` from your reported images. Mirror path mapping
(`scripts/mirror-images.sh`):

- `docker.io/<repo>:<tag>` → `<registry>/docker-hub/<repo>:<tag>`
- `quay.io/<repo>:<tag>` → `<registry>/quay/<repo>:<tag>`
- `ghcr.io/<repo>:<tag>` → `<registry>/ghcr/<repo>:<tag>`

Reference ECR images by the mirrored path. Report the upstream refs you used.

## Database (RDS PostgreSQL 18.4, single instance)

- Endpoint: `terraform -chdir=terraform output -raw rds_endpoint` (host only).
- Master creds: Secrets Manager `<CLUSTER_NAME>/rds/master` (JSON:
  `username`,`password`,`host`,`port`). Master user `dbadmin`.
- Logical databases (the orchestrator's db-init job creates these + roles):
  - `coder` (already the instance default db)
  - `keycloak`
  - `gitlabhq_production`
- Assume each app reads its DB password from a k8s Secret named
  `<app>-db` (key `password`) that the platform layer will create. Declare the
  Secret name you expect; do not invent passwords.

## AI path (Coder AI Gateway)

Three providers configured (anthropic-direct, openai-direct, anthropic-bedrock); AI Governance Add-On license is present.

1. **Anthropic-direct (PRIMARY for demo reliability)**: points at
   `api.anthropic.com`; egress leaves the VPC via the NAT gateway. API key
   comes from a k8s Secret (key `ANTHROPIC_API_KEY`); never hardcode it.
2. **Bedrock (in-boundary, SECONDARY)**: IRSA, no static keys. The Coder
   service account `coder/coder` is annotated with
   `eks.amazonaws.com/role-arn: arn:aws-us-gov:iam::<ACCOUNT_ID>:role/<CLUSTER_NAME>-coder-bedrock`.
   Region `us-gov-west-1`; model
   `us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0`; Nova Pro
   (`amazon.nova-pro-v1:0`) is the small/fast fallback. Bedrock is ENABLED
   and verified live (HTTP 200) on Coder v2.34.1, which backports the SigV4
   proxy-header fix (#26053).
3. **OpenAI-direct**: points at `api.openai.com`; API key from a k8s Secret.
   Reconciled via the Coder AI Providers API (`/api/v2/ai/providers`),
   not the seeded Helm env.

Verify exact env var / values schema against
`https://coder.com/docs/ai-coder/ai-gateway` (provider env vars like
`BEDROCK_REGION`, `BEDROCK_MODEL`, and indexed
`CODER_AI_GATEWAY_<TYPE>_<INDEX>_<FIELD>`). Client uses
`ANTHROPIC_BASE_URL=<access-url>/api/v2/aibridge/anthropic` +
`ANTHROPIC_AUTH_TOKEN=<coder session token>`.

## Coder server env (highlights)

- `CODER_ACCESS_URL=https://dev.<BASE_DOMAIN>`
- `CODER_WILDCARD_ACCESS_URL=*.<BASE_DOMAIN>`
- OIDC via Keycloak realm `coder`, client `coder`, issuer
  `https://auth.<BASE_DOMAIN>/realms/coder`.

## Directory ownership

| Dir | Workstream |
|---|---|
| `deploy/coder/` | Coder Helm values + Ingress |
| `deploy/keycloak/` | Keycloak Deployment/Service/Ingress + realm import |
| `deploy/gitlab/` | GitLab single-container + Ingress |
| `coder-templates/claude-code/` | Workspace template (Coder Agents + Claude Code) |
| `deploy/platform/` , `scripts/images.txt` | Orchestrator (do not edit) |

## Secrets management

No plaintext secrets in git. Real secrets live in AWS Secrets Manager under
`<CLUSTER_NAME>/*` and are synced into Kubernetes by the External Secrets
Operator (ESO) via IRSA. The `*.example.yaml` files carry only placeholders,
never real values. Secret scanning runs through gitleaks both in pre-commit
(opt-in via `pre-commit install`) and in CI on every pull request and push to
`main`.
