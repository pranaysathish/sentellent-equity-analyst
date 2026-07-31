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

  # Points at the managed instance rather than a container on the host, so
  # replacing the instance no longer destroys the data.
  database_url = "postgresql://sentellent:${local.db_password}@${aws_db_instance.main.endpoint}/sentellent"

  gh_owner = split("/", var.github_repository)[0]
  gh_repo  = split("/", var.github_repository)[1]

  # The single public address of the application. Everything that needs to
  # know where the app lives — the OAuth redirect URI, the frontend's own
  # base URL, the deploy verification step — derives from this one value, so
  # switching front doors is a one-line change.
  # API Gateway returns its invoke URL with a trailing slash; left in, every
  # derived URL would contain a double slash ("...amazonaws.com//api/...")
  # and the OAuth redirect would not match what Google has registered.
  public_base_url = var.enable_cloudfront ? (
    "https://${aws_cloudfront_distribution.main[0].domain_name}"
    ) : (
    trimsuffix(aws_apigatewayv2_stage.default.invoke_url, "/")
  )
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
