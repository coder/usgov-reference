# Keycloak (`auth.<BASE_DOMAIN>`)

Keycloak **26.6.3** for the GovCloud Coder demo. Runs in namespace `keycloak`,
behind the locked ingress path:

```
client --HTTPS--> NLB (TLS terminated, ACM cert) --HTTP--> ingress-nginx --HTTP--> keycloak:8080
```

Backed by the shared RDS PostgreSQL 18.4 instance (logical database `keycloak`).
Provides OIDC SSO for Coder (`dev.<BASE_DOMAIN>`) via realm `coder`.

## Files

| File | Purpose |
|---|---|
| `deployment.yaml` | `ServiceAccount` + `Deployment` (Keycloak 26.6.3, postgres, proxy/hostname/health config) |
| `service.yaml` | `ClusterIP` on `8080` (management `9000` deliberately not exposed) |
| `ingress.yaml` | `ingressClassName: nginx`, host `auth.<BASE_DOMAIN>`, plain-HTTP backend |
| `realm-coder.json` | Realm `coder`: confidential client `coder` + `demo` user + token settings |
| `secrets.example.yaml` | Placeholder `keycloak-db` / `keycloak-admin` Secrets (REPLACE_ME) |
| `kustomization.yaml` | Wires the manifests + generates the realm-import ConfigMap from the JSON |

## Image

- Upstream (pinned): `quay.io/keycloak/keycloak:26.6.3`
- Referenced as ECR mirror: `<ACCOUNT_ID>.dkr.ecr.us-gov-west-1.amazonaws.com/quay/keycloak/keycloak:26.6.3`
- Add `quay.io/keycloak/keycloak:26.6.3` to `scripts/images.txt` (orchestrator-owned) so the mirror job pulls it.

## Verified Keycloak 26.x configuration

26.x changed several knobs from older majors. What this manifest uses and why
(docs: <https://www.keycloak.org/server/reverseproxy>,
<https://www.keycloak.org/server/hostname>,
<https://www.keycloak.org/server/configuration>,
<https://www.keycloak.org/observability/health>,
<https://www.keycloak.org/server/containers>):

| Setting | Value | Notes |
|---|---|---|
| `KC_PROXY_HEADERS` | `xforwarded` | Replaces the removed `KC_PROXY=edge` (deprecated since v24). Parses `X-Forwarded-*`. |
| `KC_HTTP_ENABLED` | `true` | Required when TLS terminates at the proxy (edge termination). |
| `KC_HOSTNAME` | `https://auth.<BASE_DOMAIN>` | hostname v2: a full URL fixes scheme/host/port. Chosen because the L4 NLB terminates TLS and does not inject a trustworthy `X-Forwarded-Proto`. With a full URL, `hostname-strict` stays at its secure default (`true`); we do **not** set it to `false`. |
| `KC_DB` | `postgres` | Build-time option (applied by the implicit build on `start`). |
| `KC_DB_URL` | `jdbc:postgresql://REPLACE_WITH_RDS_ENDPOINT:5432/keycloak` | Full JDBC URL to the RDS endpoint + `keycloak` db. |
| `KC_DB_USERNAME` / `KC_DB_PASSWORD` | from Secret `keycloak-db` | Keys `username` / `password`. |
| `KC_HEALTH_ENABLED` / `KC_METRICS_ENABLED` | `true` | Build-time options; expose `/health` + `/metrics` on management port **9000**. |
| `KC_CACHE` | `local` | Single replica; avoids the default `jdbc-ping` cluster discovery. |
| `KC_BOOTSTRAP_ADMIN_USERNAME` / `KC_BOOTSTRAP_ADMIN_PASSWORD` | from Secret `keycloak-admin` | **Renamed in 26.0** from `KEYCLOAK_ADMIN` / `KEYCLOAK_ADMIN_PASSWORD`. First-boot only. |

Health endpoints on `:9000` (probes in `deployment.yaml`):
`/health/started` (startup), `/health/live` (liveness), `/health/ready` (readiness).

### `start` vs `start --optimized`

This manifest uses **`start --import-realm`** (not `--optimized`).

`KC_DB`, `KC_HEALTH_ENABLED`, `KC_METRICS_ENABLED`, and `KC_CACHE` are
**build-time** options. `--optimized` tells Keycloak to skip the build and
assume a pre-built image. The stock upstream image we mirror is **not** built
for postgres, so `start --optimized` would ignore `KC_DB` and fall back to the
H2 dev database. Plain `start` runs the build automatically on first boot
(slower start, hence the generous `startupProbe`), which is correct for an
unmodified mirrored image.

To switch to `--optimized` later, bake a custom image
(`FROM .../quay/keycloak/keycloak:26.6.3` + `RUN kc.sh build --db=postgres
--health-enabled=true --metrics-enabled=true --cache=local`), push it to ECR,
and change the args to `start --optimized --import-realm`. That introduces a
build pipeline outside the current "mirror upstream only" convention, so it is
left as a future hardening step (see open questions).

## Realm import

`--import-realm` imports every `*.json` under `/opt/keycloak/data/import` on
startup. The `kustomization.yaml` generates ConfigMap `keycloak-realm-coder`
from `realm-coder.json`, mounted read-only at that path. Import is idempotent:
if realm `coder` already exists it is skipped (logged), so leaving the flag on
across restarts is safe.

`realm-coder.json` defines:

- Confidential OIDC client `coder` (standard flow), redirect URIs
  `https://dev.<BASE_DOMAIN>/api/v2/users/oidc/callback` and
  `https://dev.<BASE_DOMAIN>/*`, web origins `+`.
- User `demo` (`demo@<BASE_DOMAIN>`, `emailVerified: true`).
- Token settings: 5-min access tokens, 30-min idle / 10-hour max SSO session.

Two placeholders in the JSON are **not** k8s Secrets and must be set before/after import:

- `coder` client `secret` -> must equal the value Coder reads from Secret
  `coder-oidc` (owned by `deploy/coder/`). Issuer for Coder:
  `https://auth.<BASE_DOMAIN>/realms/coder`.
- `demo` user password.

Alternative to `--import-realm`: run a one-off `kc.sh import --file
/opt/keycloak/data/import/realm-coder.json` as a Job, then run `start` without
the flag.

## Install order

1. Platform layer is up: `keycloak` namespace, ingress-nginx + NLB + ACM cert,
   RDS reachable, and the `keycloak` logical db + role created by the db-init job.
2. Mirror the image: ensure `quay.io/keycloak/keycloak:26.6.3` is in
   `scripts/images.txt`, then run `scripts/mirror-images.sh`.
3. Create Secrets (real values, not committed):
   ```sh
   kubectl -n keycloak create secret generic keycloak-db \
     --from-literal=username=keycloak --from-literal=password='<…>'
   kubectl -n keycloak create secret generic keycloak-admin \
     --from-literal=username=admin --from-literal=password='<…>'
   ```
4. Set the real RDS endpoint in `deployment.yaml` (`KC_DB_URL`,
   `REPLACE_WITH_RDS_ENDPOINT`) and the realm placeholders in `realm-coder.json`.
5. Apply:
   ```sh
   kubectl apply -k deploy/keycloak/
   ```
   (or apply `deployment.yaml`, `service.yaml`, `ingress.yaml` individually after
   creating the `keycloak-realm-coder` ConfigMap with
   `kubectl -n keycloak create configmap keycloak-realm-coder --from-file=deploy/keycloak/realm-coder.json`).
6. Verify: `kubectl -n keycloak rollout status deploy/keycloak`, then browse
   `https://auth.<BASE_DOMAIN>/realms/coder/.well-known/openid-configuration`.

## Open questions / risks

1. **ingress-nginx `X-Forwarded-Proto` (platform-owned).** With an L4 NLB doing
   TLS termination, the controller sees plain HTTP and forwards
   `X-Forwarded-Proto: http`. Pinning `KC_HOSTNAME` to a full `https://` URL
   makes Keycloak independent of this, and the Ingress sets
   `ssl-redirect: "false"` to avoid a redirect loop. Confirm the controller is
   not separately forcing SSL redirects for this host.
2. **`keycloak-db` username key.** CONVENTIONS only guarantees the `password`
   key. This manifest also reads `username` (role `keycloak`). If the platform
   Secret omits `username`, either add it or hardcode `KC_DB_USERNAME=keycloak`.
3. **RDS endpoint injection.** `KC_DB_URL` carries a literal placeholder. Decide
   whether the orchestrator templates this (kustomize/helm) or it is filled at
   apply time. Verify the RDS role requires no `?sslmode=` / TLS JDBC params in
   this VPC; add them to the JDBC URL if enforced.
4. **Client/user secrets in realm JSON.** `coder` client secret and `demo`
   password are committed as `REPLACE_ME` placeholders. The client secret must
   be kept in sync with Secret `coder-oidc`. A realm import cannot natively pull
   these from a k8s Secret; if that coupling is undesirable, import the realm
   without the secret and set it via `kcadm` post-import.
5. **`start` vs pre-built `--optimized`.** Demo uses plain `start` (auto-build,
   slower cold start). If startup time matters, switch to a pre-built ECR image
   (see above); needs orchestrator buy-in for a build step.
6. **Single replica.** No HA; `KC_CACHE=local`. Fine for the demo, not for prod.
