variable "region" {
  description = "GovCloud region"
  type        = string
  default     = "us-gov-west-1"
}

variable "cluster_name" {
  description = "EKS cluster name"
  type        = string
  default     = "<CLUSTER_NAME>"
}

variable "vpc_cidr" {
  description = "CIDR for the single demo VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "domain" {
  description = "Public subdomain delegated to this GovCloud account"
  type        = string
  default     = "<BASE_DOMAIN>"
}

variable "route53_zone_id" {
  description = "Hosted zone ID for var.domain (already created)"
  type        = string
  default     = "<ROUTE53_ZONE_ID>"
}

variable "acm_certificate_arn" {
  description = "ACM cert covering domain + *.domain (already issued)"
  type        = string
  default     = "arn:aws-us-gov:acm:us-gov-west-1:<ACCOUNT_ID>:certificate/<ACM_CERT_UUID>"
}

variable "bedrock_inference_profile" {
  description = "Bedrock inference profile ID for the AI Gateway allowlist"
  type        = string
  default     = "us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0"
}

variable "kubernetes_version" {
  description = "EKS Kubernetes version"
  type        = string
  default     = "1.36"
}

variable "postgres_version" {
  description = "RDS PostgreSQL engine version"
  type        = string
  default     = "18.4"
}

variable "db_instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.m6g.large"
}
