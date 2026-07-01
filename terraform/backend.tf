terraform {
  backend "s3" {
    bucket         = "<CLUSTER_NAME>-tfstate-<ACCOUNT_ID>"
    key            = "demo/terraform.tfstate"
    region         = "us-gov-west-1"
    dynamodb_table = "<CLUSTER_NAME>-tflock"
    encrypt        = true
  }
}
