output "region" {
  description = "GovCloud region the substrate is deployed in"
  value       = var.region
}

output "cluster_name" {
  description = "EKS cluster name"
  value       = aws_eks_cluster.this.name
}

output "cluster_endpoint" {
  description = "EKS API server endpoint"
  value       = aws_eks_cluster.this.endpoint
}

output "cluster_oidc_issuer" {
  description = "EKS OIDC issuer URL (used for IRSA)"
  value       = local.oidc_issuer
}

output "oidc_provider_arn" {
  description = "IAM OIDC provider ARN for the cluster"
  value       = aws_iam_openid_connect_provider.eks.arn
}

output "bedrock_role_arn" {
  description = "IRSA role ARN for the coder/coder service account to call Bedrock"
  value       = aws_iam_role.coder_bedrock.arn
}

output "rds_endpoint" {
  description = "RDS PostgreSQL endpoint (host)"
  value       = aws_db_instance.this.address
}

output "rds_secret_arn" {
  description = "Secrets Manager secret holding the RDS master credentials"
  value       = aws_secretsmanager_secret.db.arn
}

output "ecr_registry" {
  description = "Private ECR registry host for mirrored images"
  value       = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com"
}
