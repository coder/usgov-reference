provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "<CLUSTER_NAME>"
      ManagedBy = "terraform"
    }
  }
}

# Use these instead of hardcoding ARNs. GovCloud partition is aws-us-gov.
data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
