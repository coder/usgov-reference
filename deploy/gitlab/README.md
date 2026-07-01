# GitLab CE (single-container Omnibus), `gitlab.<BASE_DOMAIN>`

GitLab CE **19.0.1** deployed as the single-container Omnibus image (not the Helm
chart), in namespace `gitlab`, behind the shared NLB (TLS) + ingress-nginx.

This is a **demo** footprint: one StatefulSet replica, embedded PostgreSQL/Redis,
monitoring and extra services trimmed off.

## Topology

```
client ──HTTPS──> NLB (terminates TLS, ACM cert)
                   └─HTTP──> ingress-nginx
                              └─HTTP──> Service gitlab:80
                                         └─> Pod gitlab-0 (bundled NGINX :80 -> Workhorse/Puma)
```

TLS is terminated upstream, so the pod's bundled NGINX serves plain HTTP and we
force `X-Forwarded-Proto=https` so GitLab generates correct `https://` links and
does not redirect-loop. See the `GITLAB_OMNIBUS_CONFIG` block in
[`statefulset.yaml`](./statefulset.yaml).

## Image

| Upstream (pinned)                      | ECR (mirrored, used by manifests)                                                            |
|----------------------------------------|----------------------------------------------------------------------------------------------|
| `docker.io/gitlab/gitlab-ce:19.0.1-ce.0` | `<ACCOUNT_ID>.dkr.ecr.us-gov-west-1.amazonaws.com/docker-hub/gitlab/gitlab-ce:19.0.1-ce.0` |

Add the upstream ref to `scripts/images.txt` (orchestrator-owned) so
`scripts/mirror-images.sh` mirrors it. No other images are required: the Omnibus
image bundles NGINX, Puma, Workhorse, Sidekiq, Gitaly, Redis, and PostgreSQL.

## Prerequisites (owned by the platform layer, not this directory)

- Namespace `gitlab` exists.
- ingress-nginx is installed and the NLB is wired to the ACM cert (per
  `deploy/CONVENTIONS.md`).
- A gp3 StorageClass from EKS Auto Mode named **`auto-ebs-sc`**
  (provisioner `ebs.csi.eks.amazonaws.com`). EKS Auto Mode does not ship a
  default StorageClass, so the platform must create one. If it is named
  differently, update `storageClassName` in `statefulset.yaml`. See
  [Open questions](#open-questions--risks).
- The `gitlab-ce` image mirrored into ECR (above).

## Install order

```bash
# 1) Create the real root-password Secret (do NOT apply secrets.example.yaml as-is).
kubectl -n gitlab create secret generic gitlab-secrets \
  --from-literal=initial_root_password='<a-strong-password>'

# 2) StatefulSet (also creates the ServiceAccount + the 3 PVCs).
kubectl apply -f statefulset.yaml

# 3) Service + Ingress.
kubectl apply -f service.yaml
kubectl apply -f ingress.yaml

# 4) Watch it boot (first boot runs DB migrations; allow several minutes).
kubectl -n gitlab rollout status statefulset/gitlab --timeout=20m
kubectl -n gitlab get pods -w
```

## First login / root password

- User: `root`
- Password: the value you put in `gitlab-secrets.initial_root_password`.

If you did **not** set the Secret before first boot, GitLab auto-generates a
password and writes it to a file inside the pod (valid for ~24h after first
reconfigure):

```bash
kubectl -n gitlab exec gitlab-0 -- cat /etc/gitlab/initial_root_password
```

To change the password later (the Secret is ignored after first boot):

```bash
kubectl -n gitlab exec -it gitlab-0 -- gitlab-rake "gitlab:password:reset[root]"
```

## Database: embedded PostgreSQL (chosen) vs shared RDS

**Decision: use the embedded, bundled PostgreSQL** (the Omnibus default). Its data
persists under `/var/opt/gitlab/postgresql` on the `var-opt-gitlab` PVC.

Why embedded for this single-container demo:

- Simplest path; the Omnibus image is designed to run its own tightly-coupled
  PostgreSQL + Redis. Fewest moving parts for a time-boxed demo.
- No dependency on the orchestrator's db-init job creating the
  `gitlabhq_production` database, role, and `gitlab-db` Secret, and no GitLab
  schema migrations run against the shared RDS instance.
- Decoupled blast radius: GitLab keeps working independently of RDS health.
- Version-safe: GitLab 19 requires **PostgreSQL 17+**; the bundled engine always
  satisfies this with no drift risk.

Tradeoff: GitLab's data is not under RDS automated backups/Multi-AZ; durability
relies on the EBS PVC plus GitLab's own backup tooling
(`gitlab-backup`). Acceptable for a demo. The shared RDS is **PostgreSQL 18.4**,
which also satisfies the 17+ minimum if you later want managed storage.

### Switching to shared RDS (alternative, not enabled)

If you ever need managed storage, disable the embedded Postgres and point GitLab
at RDS. Add to `GITLAB_OMNIBUS_CONFIG`, and inject the password from a
platform-provided `gitlab-db` Secret (key `password`, per `deploy/CONVENTIONS.md`):

```ruby
postgresql['enable'] = false
gitlab_rails['db_adapter']  = 'postgresql'
gitlab_rails['db_host']     = '<rds_endpoint host only>'
gitlab_rails['db_port']     = 5432
gitlab_rails['db_database'] = 'gitlabhq_production'
gitlab_rails['db_username'] = 'gitlab'
gitlab_rails['db_password'] = ENV['GITLAB_DB_PASSWORD']  # from gitlab-db Secret
```

This requires the orchestrator to have created the `gitlabhq_production` database,
the `gitlab` role, and the `gitlab-db` Secret first. Redis would still be embedded.

## Optional: Keycloak OIDC (do not block the demo on this)

GitLab can SSO against Keycloak (`auth.<BASE_DOMAIN>`, realm to be confirmed).
This is optional; the root login above is enough to demo GitLab. When ready, add an
`openid_connect` provider to `GITLAB_OMNIBUS_CONFIG` (sketch, verify against the
Keycloak realm/client and store the client secret in a Secret):

```ruby
gitlab_rails['omniauth_enabled'] = true
gitlab_rails['omniauth_allow_single_sign_on'] = ['openid_connect']
gitlab_rails['omniauth_block_auto_created_users'] = false
gitlab_rails['omniauth_providers'] = [
  {
    name: 'openid_connect',
    label: 'Keycloak',
    args: {
      name: 'openid_connect',
      scope: ['openid', 'profile', 'email'],
      response_type: 'code',
      issuer: 'https://auth.<BASE_DOMAIN>/realms/<realm>',
      discovery: true,
      client_auth_method: 'query',
      uid_field: 'preferred_username',
      client_options: {
        identifier: 'gitlab',
        secret: ENV['GITLAB_OIDC_CLIENT_SECRET'],
        redirect_uri: 'https://gitlab.<BASE_DOMAIN>/users/auth/openid_connect/callback'
      }
    }
  }
]
```

## Open questions / risks

1. **StorageClass name.** Manifests assume `auto-ebs-sc` (the AWS-documented EKS
   Auto Mode gp3 class). Confirm the exact name the platform layer created; the
   `deploy/CONVENTIONS.md` text says "gp3 storage class" generically. If wrong,
   the three PVCs stay `Pending`.
2. **Git over SSH is not exposed.** The NLB only terminates 443; there is no
   path for git+SSH (port 22). Clone/push over HTTPS works. Wire SSH later if the
   demo needs it (separate NLB listener + Service of type LoadBalancer or a TCP
   ingress).
3. **Resource sizing.** Requests 1 CPU / 4Gi, limits 2 CPU / 8Gi. If the node is
   tight, boots get slower and OOM risk rises; tune in `statefulset.yaml`.
4. **First-boot time.** GitLab can take several minutes (migrations); the startup
   probe allows ~15 min. Do not mistake a slow first boot for a failure.
5. **Keycloak realm/client** for the optional OIDC block above is unconfirmed.
6. **Backups.** With embedded Postgres, there is no managed backup. Add
   `gitlab-backup` + an S3 target if the data must survive PVC loss.

---

*Authored by Coder Agents. Scope: `deploy/gitlab/` only.*
