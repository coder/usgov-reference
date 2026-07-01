# IRSA (IAM Roles for Service Accounts): wire the EKS OIDC provider to an IAM
# role the Coder service account can assume to call Bedrock, with no static keys.
locals {
  oidc_issuer      = aws_eks_cluster.this.identity[0].oidc[0].issuer
  oidc_issuer_host = replace(local.oidc_issuer, "https://", "")

  # Strip the cross-region "us-gov." routing prefix from the inference profile
  # to get the underlying foundation-model id used in foundation-model ARNs.
  bedrock_base_model = replace(var.bedrock_inference_profile, "us-gov.", "")

  # The us-gov. inference profile is a cross-region profile that can route to
  # both GovCloud regions, so InvokeModel must be permitted on the profile
  # (called in-region) plus the underlying foundation model in every region it
  # can reach. The Nova Pro fallback is invoked directly, in-region only.
  bedrock_resource_arns = concat(
    [
      "arn:${data.aws_partition.current.partition}:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_inference_profile}",
    ],
    [
      for r in ["us-gov-west-1", "us-gov-east-1"] :
      "arn:${data.aws_partition.current.partition}:bedrock:${r}::foundation-model/${local.bedrock_base_model}"
    ],
    [
      "arn:${data.aws_partition.current.partition}:bedrock:${var.region}::foundation-model/amazon.nova-pro-v1:0",
    ],
  )
}

# OIDC provider for the cluster, so AWS trusts EKS-issued service account tokens.
data "tls_certificate" "oidc" {
  url = local.oidc_issuer
}

resource "aws_iam_openid_connect_provider" "eks" {
  url             = local.oidc_issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.oidc.certificates[0].sha1_fingerprint]
}

# Trust policy: only the coder/coder service account may assume this role.
data "aws_iam_policy_document" "coder_bedrock_assume" {
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
      values   = ["system:serviceaccount:coder:coder"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "coder_bedrock" {
  name               = "${var.cluster_name}-coder-bedrock"
  assume_role_policy = data.aws_iam_policy_document.coder_bedrock_assume.json
}

# Least-privilege Bedrock invoke permissions, scoped to the demo models only.
data "aws_iam_policy_document" "coder_bedrock" {
  statement {
    sid    = "InvokeAllowlistedBedrockModels"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = local.bedrock_resource_arns
  }
}

resource "aws_iam_role_policy" "coder_bedrock" {
  name   = "bedrock-invoke"
  role   = aws_iam_role.coder_bedrock.id
  policy = data.aws_iam_policy_document.coder_bedrock.json
}
