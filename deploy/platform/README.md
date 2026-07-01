# Platform layer (orchestrator-owned)

Brings up the shared cluster platform that every app layer depends on:
node group, addons, storage, ingress + NLB, RDS roles/databases, and
workspace RBAC. These steps were executed live against the cluster during the
overnight build; this README is the reproducible record.

> **Context:** EKS Auto Mode node provisioning is broken in this GovCloud
> account (the AWS-managed `AWSServiceRoleForAmazonEKS` SLR lacks
> `iam:AddRoleToInstanceProfile` / `iam:TagInstanceProfile`, so Auto Mode
> NodeClass validation never succeeds). The cluster was converted to standard
> EKS. The items below are not yet in `terraform/`; see `STATUS.md`
> "Deviations to reconcile into Terraform".

Prereqs for every command:

```sh
. ~/.config/<CLUSTER_NAME>/env          # AWS_PROFILE, region (sh: use ".", not "source")
export KUBECONFIG=./kubeconfig
```

## 1. Compute: disable Auto Mode, create a managed node group

```sh
aws eks update-cluster-config --name <CLUSTER_NAME> \
  --compute-config enabled=false \
  --storage-config '{"blockStorage":{"enabled":false}}' \
  --kubernetes-network-config '{"elasticLoadBalancing":{"enabled":false}}'

# Node role <CLUSTER_NAME>-mngnode: AmazonEKSWorkerNodePolicy, AmazonEKS_CNI_Policy,
# AmazonEC2ContainerRegistryReadOnly, AmazonSSMManagedInstanceCore, AmazonEBSCSIDriverPolicy.
# Managed node group `mng`: 3x m5.xlarge, AL2023_x86_64_STANDARD, private subnets,
# min 2 / desired 3 / max 4.
```

## 2. Addons

```sh
# vpc-cni, kube-proxy, coredns: default config.
# aws-ebs-csi-driver: needs IRSA (node IMDS hop limit blocks the controller's
# default credential path), so it gets its own role:
aws iam create-role --role-name <CLUSTER_NAME>-ebs-csi \
  --assume-role-policy-document file://ebs-trust.json   # trusts the cluster OIDC provider,
                                                         # sub system:serviceaccount:kube-system:ebs-csi-controller-sa
aws iam attach-role-policy --role-name <CLUSTER_NAME>-ebs-csi \
  --policy-arn arn:aws-us-gov:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy
aws eks update-addon --cluster-name <CLUSTER_NAME> --addon-name aws-ebs-csi-driver \
  --service-account-role-arn arn:aws-us-gov:iam::<ACCOUNT_ID>:role/<CLUSTER_NAME>-ebs-csi \
  --resolve-conflicts PRESERVE
```

`gp3` is the default StorageClass (provisioner `ebs.csi.aws.com`, encrypted,
`WaitForFirstConsumer`).

## 3. Ingress + NLB

The AWS Load Balancer Controller (Helm, `kube-system`) provisions an
internet-facing NLB for the ingress-nginx controller Service. TLS terminates at
the NLB with the shared ACM cert; backends are plain HTTP.

```sh
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx --create-namespace --version 4.15.1 \
  --values ingress-nginx-values.yaml
```

The controller Service uses `aws-load-balancer-type: external` so the LB
controller (not the in-tree provider) manages the NLB. Public subnets are
auto-discovered via the `kubernetes.io/role/elb=1` tag.

## 4. DNS

Route53 alias A records in zone `<ROUTE53_ZONE_ID>` point `dev`, `auth`,
`gitlab`, and `*` (workspace apps) at the ingress NLB. In-cluster hairpin to
these public hostnames is verified (valid TLS), so Coder's server-side OIDC
calls and workspace agents work.

## 5. RDS roles + databases

Run in-cluster (RDS is private; the workspace cannot reach it directly). A Job
using the mirrored `postgres:18-alpine` image connects as the master user and
creates roles + databases. Idempotent. Note `rds.force_ssl=1`, so all clients
use TLS (`sslmode=require` / JDBC `?sslmode=require`).

- Role `coder` owns database `coder` (and its `public` schema).
- Role `keycloak` owns database `keycloak`.
- GitLab uses the Omnibus **embedded** Postgres (no RDS database).

RDS requires the master user to be a member of a role before transferring
ownership to it (`GRANT <role> TO dbadmin;`).

## 6. Application secrets

Created imperatively (never committed). See each app's `secrets.example.yaml`:
`coder-db`, `coder-oidc`, `coder-ai` (coder ns); `keycloak-db`, `keycloak-admin`
(keycloak ns); `gitlab-secrets` (gitlab ns). Values are in
`~/.config/<CLUSTER_NAME>/generated-secrets.env`.

## 7. Workspace RBAC

`workspace-rbac.yaml` grants the `coder/coder` ServiceAccount permission to
manage pods/PVCs in `coder-workspaces` (the Helm chart only grants this in the
release namespace).

```sh
kubectl apply -f workspace-rbac.yaml
```
