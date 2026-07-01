# =============================================================================
# secrets-hardening.tf - IaC for the secrets-management hardening.
# =============================================================================
# RECONCILIATION BACKLOG. These resources describe the desired state. The live
# environment was built imperatively (the External Secrets Operator IAM role was
# created with the AWS CLI; see scripts and docs/as-built/85-secrets-management.md),
# so on a reconciliation pass the existing role must be imported before apply:
#
#   terraform import aws_iam_role.external_secrets <CLUSTER_NAME>-external-secrets
#
# Reuses aws_iam_openid_connect_provider.eks and locals from irsa.tf.

# --- External Secrets Operator IRSA role ------------------------------------
# ESO's controller ServiceAccount (external-secrets/external-secrets) assumes
# this role to read demo secrets from AWS Secrets Manager. No static AWS keys.
data "aws_iam_policy_document" "external_secrets_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    effect  = "Allow"

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.eks.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:sub"
      values   = ["system:serviceaccount:external-secrets:external-secrets"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "external_secrets" {
  name               = "${var.cluster_name}-external-secrets"
  assume_role_policy = data.aws_iam_policy_document.external_secrets_assume.json
  description        = "External Secrets Operator: read <CLUSTER_NAME>/* from Secrets Manager (IRSA)"
}

# Least-privilege: read only the demo's secrets, no write, no other prefixes.
data "aws_iam_policy_document" "external_secrets" {
  statement {
    sid    = "ReadDemoSecrets"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
    ]
    resources = [
      "arn:${data.aws_partition.current.partition}:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:<CLUSTER_NAME>/*",
    ]
  }
}

resource "aws_iam_role_policy" "external_secrets" {
  name   = "secretsmanager-read"
  role   = aws_iam_role.external_secrets.id
  policy = data.aws_iam_policy_document.external_secrets.json
}

# --- EKS Secrets envelope encryption (customer-managed KMS) ------------------
# Backlog hardening: encrypt Kubernetes Secrets at rest in etcd with a CMK, on
# top of the default AWS-managed etcd encryption. Enabling envelope encryption
# on a cluster is IRREVERSIBLE and triggers a re-encrypt, so it is NOT applied
# yet; it needs an explicit maintenance decision. To enable, create the key
# below and add an `encryption_config` block to aws_eks_cluster.this in eks.tf:
#
#   encryption_config {
#     provider { key_arn = aws_kms_key.eks_secrets.arn }
#     resources = ["secrets"]
#   }
#
# Once the key exists, this can also be enabled out of band with:
#   aws eks associate-encryption-config --cluster-name <CLUSTER_NAME> \
#     --encryption-config '[{"provider":{"keyArn":"<arn>"},"resources":["secrets"]}]'
resource "aws_kms_key" "eks_secrets" {
  description             = "${var.cluster_name} EKS Secrets envelope encryption"
  deletion_window_in_days = 14
  enable_key_rotation     = true
  tags = {
    Name = "${var.cluster_name}-eks-secrets"
  }
}

resource "aws_kms_alias" "eks_secrets" {
  name          = "alias/${var.cluster_name}-eks-secrets"
  target_key_id = aws_kms_key.eks_secrets.key_id
}
