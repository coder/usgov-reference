locals {
  managed_policy_prefix = "arn:${data.aws_partition.current.partition}:iam::aws:policy"
}

# --- EKS cluster role (Auto Mode needs the compute/storage/lb/networking policies) ---
data "aws_iam_policy_document" "cluster_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  name               = "${var.cluster_name}-cluster"
  assume_role_policy = data.aws_iam_policy_document.cluster_assume.json
}

resource "aws_iam_role_policy_attachment" "cluster" {
  for_each = toset([
    "${local.managed_policy_prefix}/AmazonEKSClusterPolicy",
    "${local.managed_policy_prefix}/AmazonEKSComputePolicy",
    "${local.managed_policy_prefix}/AmazonEKSBlockStoragePolicy",
    "${local.managed_policy_prefix}/AmazonEKSLoadBalancingPolicy",
    "${local.managed_policy_prefix}/AmazonEKSNetworkingPolicy",
  ])
  role       = aws_iam_role.cluster.name
  policy_arn = each.value
}

# --- EKS Auto Mode node role (minimal) ---
data "aws_iam_policy_document" "node_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "${var.cluster_name}-node"
  assume_role_policy = data.aws_iam_policy_document.node_assume.json
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "${local.managed_policy_prefix}/AmazonEKSWorkerNodeMinimalPolicy",
    "${local.managed_policy_prefix}/AmazonEC2ContainerRegistryPullOnly",
  ])
  role       = aws_iam_role.node.name
  policy_arn = each.value
}
