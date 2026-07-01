# Coder control plane (`deploy/coder/`)

Helm values + Ingress for the Coder dashboard at `dev.<BASE_DOMAIN>`,
pinned to **Coder v2.34.1** (official chart, `ghcr.io/coder/coder:v2.34.1`
mirrored to ECR). v2.34.1 is pinned for the Bedrock SigV4 signing fix
(#26053). Read [`deploy/CONVENTIONS.md`](../CONVENTIONS.md) first.

## Files

| File                   | Purpose                                                        |
|------------------------|----------------------------------------------------------------|
| `values.yaml`          | Helm values for the Coder chart (Deployment, SA, Service, Ingress, env). |
| `secrets.example.yaml` | Placeholder manifests for the 3 Secrets `values.yaml` consumes. |
| `README.md`            | This file.                                                     |

## What the platform layer must provide first

This workstream declares only Coder objects. Before installing, the
orchestrator/platform layer must have:

- The `coder` namespace.
- `ingress-nginx` installed, fronted by the internet-facing **NLB + ACM cert**
  (TLS terminates at the NLB; backends are plain HTTP).
- The ECR mirror populated with `ghcr/coder/coder:v2.34.1`
  (`scripts/mirror-images.sh`).
- The `coder` logical DB + role on RDS (db-init job).
- The three Secrets in `secrets.example.yaml` (`coder-db`, `coder-oidc`,
  `coder-ai`), or you create them by hand (below).

## Install order

```sh
# 0. Context: point kubectl at the EKS cluster (platform layer owns this).

# 1. Namespace (skip if the platform layer already created it).
kubectl create namespace coder

# 2. Secrets. Prefer imperative creation so secrets never touch git.
#    Replace placeholders with real values first.
kubectl create secret generic coder-db -n coder \
  --from-literal=url='postgres://coder:PASSWORD@RDS_ENDPOINT:5432/coder?sslmode=require'
kubectl create secret generic coder-oidc -n coder \
  --from-literal=client-secret='KEYCLOAK_CODER_CLIENT_SECRET'
kubectl create secret generic coder-ai -n coder \
  --from-literal=ANTHROPIC_API_KEY='sk-ant-...'
#    (Or: edit secrets.example.yaml, then `kubectl apply -n coder -f secrets.example.yaml`.)

# 3. Add the chart repo and install/upgrade.
helm repo add coder-v2 https://helm.coder.com/v2
helm repo update
helm upgrade --install coder coder-v2/coder \
  --namespace coder \
  --version 2.34.1 \
  --values deploy/coder/values.yaml

# 4. Apply the AI Governance Add-On license (see "Licensing" below).
```

`RDS_ENDPOINT` (host only) comes from
`terraform -chdir=terraform output -raw rds_endpoint`. At authoring time the
Terraform apply had not run (plan: 39 to add), so `secrets.example.yaml` uses a
`REPLACE_ME_RDS_ENDPOINT` placeholder.

## How values map to the demo

| Requirement | Where in `values.yaml` |
|---|---|
| Dashboard host `dev.<BASE_DOMAIN>` | `coder.ingress.host` + `CODER_ACCESS_URL` |
| Workspace-app wildcard `*.<BASE_DOMAIN>` | `coder.ingress.wildcardHost` + `CODER_WILDCARD_ACCESS_URL` (single-level, matches the one ACM cert) |
| TLS terminated upstream at NLB | `coder.ingress.tls.enable: false`, no `coder.tls.secretNames`, `ssl-redirect: "false"` |
| Sits behind ingress-nginx (no 2nd LB) | `coder.service.type: ClusterIP` (chart default is `LoadBalancer`) |
| Postgres `coder` DB | `CODER_PG_CONNECTION_URL` from Secret `coder-db` key `url` |
| Keycloak SSO | `CODER_OIDC_*` env; client secret from Secret `coder-oidc` key `client-secret` |
| Bedrock IRSA | `coder.serviceAccount.annotations[eks.amazonaws.com/role-arn]` |
| AI Gateway providers | `CODER_AI_GATEWAY_PROVIDER_0_*` (Anthropic-direct) + `_1_*` (Bedrock) |

## AI Gateway provider schema (verified)

Verified against **v2.34.0** source and docs (provider schema unchanged in v2.34.1):

- Docs: <https://coder.com/docs/ai-coder/ai-gateway/setup> and
  `.../ai-gateway/providers` (the AI Gateway product was formerly "AI Bridge";
  API paths still use `/api/v2/aibridge/...`).
- Parser: `cli/server.go` `readAIProvidersForPrefix` (env prefix
  `CODER_AI_GATEWAY_PROVIDER_`).
- Seeding/type resolution: `coderd/ai_providers_migrate.go`
  `SeedAIProvidersFromEnv`.

Indexed scheme is `CODER_AI_GATEWAY_PROVIDER_<N>_<FIELD>` (literal word
`PROVIDER`, numeric index `<N>` starting at 0). Recognized `<FIELD>` keys:

```
TYPE                         # openai | anthropic | bedrock | azure | google |
                             # openai-compat | openrouter | vercel | copilot
NAME                         # unique, lowercase, hyphenated (routing id)
KEY | KEYS                   # bearer key(s); KEYS is comma-separated (max 5)
BASE_URL
BEDROCK_BASE_URL
BEDROCK_REGION
BEDROCK_ACCESS_KEY | BEDROCK_ACCESS_KEYS
BEDROCK_ACCESS_KEY_SECRET | BEDROCK_ACCESS_KEY_SECRETS
BEDROCK_MODEL
BEDROCK_SMALL_FAST_MODEL
```

Notes:

- The convention's guessed `CODER_AI_GATEWAY_<TYPE>_<INDEX>_<FIELD>` shape is
  **not** what v2.34 uses; the real prefix is `CODER_AI_GATEWAY_PROVIDER_<N>_`.
- A Bedrock provider can be declared as `TYPE=bedrock` (used here, most
  self-documenting) **or** `TYPE=anthropic` with `BEDROCK_*` fields set; the
  server detects "Bedrock" whenever `BEDROCK_REGION`/`BEDROCK_BASE_URL`/access
  keys are present (`IsBedrockConfigured`). Both seed an equivalent provider
  that routes through aibridge's Anthropic client.
- Do **not** attach a key to the Bedrock provider. With no static creds the AWS
  SDK default credential chain resolves the **IRSA** web-identity token from the
  annotated service account. The IAM role must allow `bedrock:InvokeModel` and
  `bedrock:InvokeModelWithResponseStream` (the Terraform `coder_bedrock` role
  grants exactly these for the inference profile + Nova Pro).
- Client side (set in the workspace template, not here):
  `ANTHROPIC_BASE_URL=<access-url>/api/v2/aibridge/anthropic` and
  `ANTHROPIC_AUTH_TOKEN=<coder session token>`.

### Live provider and model state (reconciled via the API)

Providers are reconciled through the Coder AI Providers API
(`/api/v2/ai/providers`), not through Helm. Enable and configure providers
(Anthropic direct, OpenAI direct, Bedrock IRSA) at
`https://dev.<BASE_DOMAIN>/ai/settings` or via the API.

### IMPORTANT: provider env vars seed the DB ONCE

Since v2.34, AI Gateway providers live in the **database**, managed at
`https://dev.<BASE_DOMAIN>/ai/settings` or the AI Providers API. The
`CODER_AI_GATEWAY_PROVIDER_*` env vars are **deprecated** and only **seed** the
DB on the first startup. After seeding:

- The database is authoritative; editing a provider in the dashboard is **not**
  overwritten by env on restart.
- **Changing a seeded env var later makes `coderd` fail to start** (drift guard).
  To rotate the Anthropic key or change a model, do it in `/ai/settings`, then
  update/remove the matching env var to match (or remove the env vars entirely
  once seeded).

This matters for Helm: a later `helm upgrade` that changes any
`CODER_AI_GATEWAY_PROVIDER_*` value (or the `coder-ai` secret contents) will
break startup unless you first reconcile the change in the dashboard. Treat
these values as one-time seed config.

## Licensing (AI Governance Add-On)

AI Gateway requires the **AI Governance Add-On** license. There is **no
`CODER_LICENSE` server env var** in v2.34 (the chart/server does not read a
license from env or a Secret). The license is a JWT applied at runtime and
stored in the DB. Apply it after install via CLI or UI:

```sh
# CLI (as a Coder admin/owner):
coder licenses add -f /path/to/coder.lic
# or paste the JWT in the dashboard: Admin settings > Licenses > Add a license.
```

Confirm the add-on is active before relying on AI Gateway. `/ai/settings` is
inaccessible / providers will not serve without the add-on entitlement.

## Open questions / risks

1. **`coder-db` secret shape.** `values.yaml` expects Secret `coder-db` with a
   full connection URL under key `url`. CONVENTIONS says the platform creates
   `<app>-db` with key `password` only. Reconcile with the platform layer:
   either it also publishes `url`, or add a small step to assemble the URL from
   `password` + `rds_endpoint`. (Documented in `secrets.example.yaml`.)
2. **Bedrock enabled and verified.** Claude Sonnet 4.5 on the GovCloud
   `us-gov.anthropic.claude-sonnet-4-5-...` cross-region inference profile is
   active in `us-gov-west-1`, and the Bedrock provider is enabled. Verified
   live on v2.34.1 (2026-06-08): `InvokeModel` via
   `/api/v2/aibridge/anthropic-bedrock/v1/messages` returns 200 for the
   blocking, streaming (SSE), and `anthropic-beta` header paths Claude Code
   uses. Nova Pro (`amazon.nova-pro-v1:0`) remains the configured small/fast
   fallback.
3. **Bedrock SigV4 signing fix (resolved in v2.34.1).** On v2.34.0 the Bedrock
   path failed with a SigV4 403 (`signature does not match`): the AI Gateway
   signed egress requests that still carried inbound proxy headers
   (`x-forwarded-for`, `x-envoy-*`, `x-request-id`), so the canonical
   `SignedHeaders` never matched what Bedrock recomputed. Fixed upstream by
   `coder/coder#26019` (strip proxy headers before signing), shipped in v2.34.1
   via backport #26053. The earlier `coder/aibridge#221` `anthropic-beta`
   rejection no longer reproduces on v2.34.1.
4. **IRSA STS in GovCloud.** IRSA exchanges the SA token via
   `AssumeRoleWithWebIdentity`. `AWS_REGION` + `AWS_STS_REGIONAL_ENDPOINTS=regional`
   are set so the SDK uses the GovCloud regional STS endpoint; verify the role
   trust policy lists the cluster OIDC provider and the `coder:coder` SA once the
   cluster is up.
5. **Provider seeding vs. Helm drift.** See the boxed note above. Decide the
   long-term source of truth (dashboard) and keep Helm env values frozen after
   first boot, or remove them post-seed.
6. **Could not verify live.** Terraform had not been applied (no AWS creds in
   this sandbox), so RDS endpoint, the OIDC client secret, and IRSA end-to-end
   were not exercised. Values use documented placeholders.
