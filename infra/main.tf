terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.80" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
    tls    = { source = "hashicorp/tls", version = "~> 4.0" }
  }

  # State is local by default so a first `terraform apply` needs no
  # pre-existing bucket. For team use, uncomment and point at an S3 backend.
  # backend "s3" {
  #   bucket = "sentellent-tfstate"
  #   key    = "prod/terraform.tfstate"
  #   region = "ap-south-1"
  # }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Purpose   = "sentellent-hiring-challenge"
    }
  }
}

# CloudFront's own certificate and some global resources live in us-east-1.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}

data "aws_caller_identity" "current" {}

locals {
  name = var.project

  # An explicitly supplied password wins; otherwise one is generated and kept
  # in state. Plain ternary rather than coalesce(), which has surprising
  # behaviour around empty strings and nulls.
  db_password = var.db_password != "" ? var.db_password : random_password.db.result

  database_url = "postgresql://sentellent:${local.db_password}@db:5432/sentellent"
}

resource "random_password" "db" {
  length  = 32
  special = false # keeps the value safe to embed in a connection URL unescaped
}

resource "random_password" "session_secret" {
  length  = 64
  special = false
}

resource "random_password" "internal_token" {
  length  = 40
  special = false
}
