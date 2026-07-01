# Architecture overview

This document describes the high-level design of the reference Coder
deployment on AWS GovCloud EKS. All environment-specific values (account
IDs, domain names, certificate ARNs, VPC/subnet/SG IDs, etc.) are
represented as `<PLACEHOLDER>` tokens. Fill in the real values via
`values/env.example.yaml` when standing up your own environment.

---

## Goals

- Operate a durable, multi-AZ Coder control plane in an AWS GovCloud region.
- Provide a single governance and audit plane that spans multiple workspace
  fabrics (EKS workspaces and optionally OpenShift workspaces).
- Integrate with an in-boundary identity provider (Keycloak + OIDC), an
  in-boundary source-control server (GitLab), and a cloud AI service (AWS
  Bedrock) without transmitting credentials over the public internet.

---

## High-level diagram

```
Route53 GovCloud zone: <BASE_DOMAIN>
(NS-delegated from a commercial parent zone)
          |
    +-----+-------+----------+------------+
    |             |          |            |
 dev.*          auth.*  metrics.*     gitlab.*
(Coder)      (Keycloak) (Grafana)     (EC2 ALB)

+---------------- AWS GovCloud <REGION> ------------------------------------------+
|                                                                                   |
|  EKS cluster: <CLUSTER_NAME>  (3 private subnets, 3 AZs)                        |
|                                                                                   |
|    internet-facing NLB (ACM cert: <ACM_CERT_UUID>)                               |
|      -> ingress-nginx controller                                                  |
|         -> Coder control plane (Deployment, ClusterIP)                           |
|         -> Keycloak (Deployment, ClusterIP)                                      |
|         -> Grafana  (Deployment, ClusterIP)                                      |
|         -> Istio ingressgateway (Phase 2+)                                       |
|                                                                                   |
|    Coder control plane                                                            |
|      ServiceAccount annotated for IRSA -> Bedrock IRSA role                      |
|      Reads DB credentials from k8s Secret (synced from ASM by ESO)               |
|                                                                                   |
|    Keycloak                                                                       |
|      Realm: <KEYCLOAK_REALM>   OIDC provider for Coder                           |
|      DB: RDS PostgreSQL (<RDS_ENDPOINT_HOST>:5432)                               |
|                                                                                   |
|    Istio service mesh (Phase 2)                                                   |
|      mTLS for service-to-service traffic                                          |
|      VirtualServices for internal routing                                         |
|      DestinationRule: external DB (TLS origination)                              |
|                                                                                   |
|    External Secrets Operator                                                      |
|      IRSA role -> ASM prefix <CLUSTER_NAME>/                                     |
|      Syncs all secrets into k8s Secrets consumed by Helm releases                |
|                                                                                   |
|    Observability: Prometheus, Loki, Grafana                                       |
|      Loki log storage: S3 bucket <CLUSTER_NAME>-loki                            |
|                                                                                   |
|  RDS PostgreSQL (Multi-AZ)                                                        |
|    Endpoint: <RDS_ENDPOINT_HOST>:5432                                            |
|    Databases: coder, keycloak                                                     |
|    TLS enforced; certificates from AWS regional CA                                |
|                                                                                   |
|  ECR private registry                                                             |
|    Host: <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com                            |
|    All upstream images mirrored before deployment (no pull-through in            |
|    GovCloud); mapping documented in scripts/mirror-images.sh                     |
|                                                                                   |
|  EC2: GitLab Omnibus                                                              |
|    Hostname: gitlab.<BASE_DOMAIN>                                                |
|    In-boundary source control; GitLab Runners on EKS                            |
|    Backup: S3 + EBS snapshots                                                    |
|                                                                                   |
|  AWS Bedrock (AI workloads)                                                       |
|    Accessed via IRSA (no static keys)                                             |
|    Inference profile: <BEDROCK_INFERENCE_PROFILE>                                |
|    Coder AI Gateway routes requests through this profile                          |
|                                                                                   |
|  Terraform state                                                                  |
|    S3 bucket:       <CLUSTER_NAME>-tfstate-<ACCOUNT_ID>                         |
|    DynamoDB lock:   <CLUSTER_NAME>-tflock                                        |
|                                                                                   |
+-----------------------------------------------------------------------------------+
```

---

## Component inventory

### EKS control plane

| Resource | Value |
|---|---|
| Cluster name | `<CLUSTER_NAME>` |
| Kubernetes version | `<KUBERNETES_VERSION>` |
| Node subnets | `<PRIVATE_SUBNET_ID_AZ_A>`, `<PRIVATE_SUBNET_ID_AZ_B>`, `<PRIVATE_SUBNET_ID_AZ_C>` |
| Node security group | `<NODE_SG_ID>` |
| OIDC provider ID | `<OIDC_PROVIDER_ID>` (used in IRSA trust policies) |

### DNS and TLS

| Resource | Value |
|---|---|
| Base domain | `<BASE_DOMAIN>` |
| Route53 hosted zone | `<ROUTE53_ZONE_ID>` |
| ACM certificate | `arn:aws-us-gov:acm:<REGION>:<ACCOUNT_ID>:certificate/<ACM_CERT_UUID>` |

The certificate covers `<BASE_DOMAIN>` and `*.<BASE_DOMAIN>`. TLS
terminates at the NLB; traffic from ingress-nginx to pods is plain HTTP.

### IRSA roles

| Purpose | Role ARN |
|---|---|
| Coder (Bedrock) | `arn:aws-us-gov:iam::<ACCOUNT_ID>:role/<CLUSTER_NAME>-coder-bedrock` |
| External Secrets Operator | `arn:aws-us-gov:iam::<ACCOUNT_ID>:role/<CLUSTER_NAME>-external-secrets` |
| EBS CSI driver | `arn:aws-us-gov:iam::<ACCOUNT_ID>:role/<CLUSTER_NAME>-ebs-csi` |
| AWS Load Balancer Controller | `arn:aws-us-gov:iam::<ACCOUNT_ID>:role/<CLUSTER_NAME>-lbc` |
| Managed node group | `arn:aws-us-gov:iam::<ACCOUNT_ID>:role/<CLUSTER_NAME>-node` |

### AWS Secrets Manager paths

All secrets live under the prefix `<CLUSTER_NAME>/`. Representative paths:

| Secret | Path |
|---|---|
| Coder DB URL | `<CLUSTER_NAME>/coder/db-url` |
| Coder OIDC client secret | `<CLUSTER_NAME>/coder/oidc-client-secret` |
| Coder Anthropic API key | `<CLUSTER_NAME>/coder/anthropic-api-key` |
| GitLab root password | `<CLUSTER_NAME>/gitlab/root-password` |
| Keycloak admin password | `<CLUSTER_NAME>/keycloak/admin-password` |
| Grafana admin password | `<CLUSTER_NAME>/grafana/admin-password` |

External Secrets Operator syncs each path into a k8s Secret in the
appropriate namespace. No secret value is stored in Git.

---

## Deployment phases

| Phase | Delivers |
|---|---|
| **1** | EKS + NLB + Coder + Keycloak (minimal) + EKS provisioner/proxy + EKS workspace template |
| **2** | Istio, GitLab, full identity federation (group sync, org roles), observability polish |
| **3** | OpenShift IPI cluster + OCP provisioner/proxy + OCP workspace template |
| **4** | Bedrock IRSA + AI Gateway configuration + AI workspace templates |

Phase 1 is the minimum viable Coder environment. Later phases layer on
additional capabilities without restructuring Phase 1 resources.

---

## Durable state and disaster recovery

| Asset | Store | Notes |
|---|---|---|
| Coder database | RDS Multi-AZ | Automated snapshots; point-in-time recovery |
| Keycloak database | RDS Multi-AZ | Same instance, separate DB |
| Loki logs | S3 | Versioning enabled |
| GitLab data | S3 + EBS snapshots | Omnibus backup cron |
| Container images | ECR | Immutable tags; lifecycle policy keeps last 30 |
| Terraform state | S3 + DynamoDB | Versioning + locking |

The Coder control plane is stateless; a fresh pod reads all configuration
from environment variables and the Postgres database. A replacement cluster
(`terraform destroy` + `terraform apply`) can reconnect to the same RDS
instance and pick up exactly where the old cluster left off.

---

## Security posture highlights

- **No static AWS credentials** anywhere in the cluster. All AWS API access
  uses IRSA (IAM Roles for Service Accounts).
- **No secrets in Git.** All sensitive values live in AWS Secrets Manager
  and are synced into the cluster at runtime by the External Secrets Operator.
- **mTLS between services** (Istio, Phase 2+).
- **Private subnets only** for EKS nodes and RDS. The NLB is the sole
  internet-facing entry point.
- **ECR as the only image source** inside the cluster. All upstream images
  are mirrored before use; worker nodes have no internet egress for pulls.
- **Least-privilege IRSA roles**: each role grants only the specific
  IAM actions required by the corresponding workload.

---

## Related documents

- [`values/env.example.yaml`](../values/env.example.yaml): full placeholder
  schema with commentary for each value.
- [`CONTRIBUTING.md`](../CONTRIBUTING.md): promotion model, placeholder
  style, and CI gate description.
- [`scripts/check-identifiers.sh`](../scripts/check-identifiers.sh):
  generic identifier pattern scanner; runs in CI and locally.
