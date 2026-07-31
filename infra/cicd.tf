# Container registry and the GitHub Actions deployment identity.
#
# CI authenticates via OIDC — GitHub presents a short-lived signed token and
# assumes this role. No AWS access keys are stored in GitHub secrets, so there
# is no long-lived credential to leak or rotate.

resource "aws_ecr_repository" "api" {
  name                 = "${local.name}-api"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Untagged layers pile up fast with a per-commit tagging scheme; this keeps the
# registry (and its storage bill) bounded without any manual cleanup.
resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the 10 most recent commit-tagged images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["sha-"]
          countType     = "imageCountMoreThan"
          countNumber   = 10
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Expire untagged images after a day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
    ]
  })
}

# --------------------------------------------------------------------------- #
# GitHub OIDC
# --------------------------------------------------------------------------- #
data "tls_certificate" "github" {
  url = "https://token.actions.githubusercontent.com/.well-known/openid-configuration"
}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.github.certificates[0].sha1_fingerprint]
}

resource "aws_iam_role" "github_actions" {
  name = "${local.name}-github-actions"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        # Scoped to this repository specifically. Without this condition any
        # GitHub repo in the world could assume the role.
        #
        # Two patterns because GitHub now issues the subject claim with
        # immutable numeric IDs appended:
        #
        #   classic:   repo:owner/name:ref:refs/heads/main
        #   immutable: repo:owner@1234/name@5678:ref:refs/heads/main
        #
        # The IDs survive a rename or transfer, which is the point — they stop
        # someone claiming a freed-up repository name and inheriting its cloud
        # trust. A policy written for the classic form alone is rejected with a
        # bare "Not authorized to perform sts:AssumeRoleWithWebIdentity", which
        # gives no hint that the claim format is the problem. Both are matched
        # so this keeps working whichever form GitHub sends.
        StringLike = {
          "token.actions.githubusercontent.com:sub" = [
            "repo:${var.github_repository}:*",
            "repo:${local.gh_owner}@*/${local.gh_repo}@*:*",
          ]
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_actions" {
  name = "${local.name}-github-actions-policy"
  role = aws_iam_role.github_actions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrAuth"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "EcrPush"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:CompleteLayerUpload",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
        ]
        Resource = aws_ecr_repository.api.arn
      },
      {
        Sid      = "PublishFrontend"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:DeleteObject", "s3:ListBucket", "s3:GetObject"]
        Resource = [aws_s3_bucket.frontend.arn, "${aws_s3_bucket.frontend.arn}/*"]
      },
      {
        Sid      = "InvalidateCache"
        Effect   = "Allow"
        Action   = ["cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"]
        Resource = var.enable_cloudfront ? aws_cloudfront_distribution.main[0].arn : "*"
      },
      {
        # The instance is replaced whenever its configuration changes, so a
        # policy naming one instance ARN starts denying deploys the moment
        # that happens — and the error names only "no identity-based policy
        # allows ssm:SendCommand", which points nowhere near the real cause.
        # Scoping by tag survives replacement while still refusing every other
        # instance in the account.
        Sid      = "TriggerDeployOnTaggedInstances"
        Effect   = "Allow"
        Action   = ["ssm:SendCommand"]
        Resource = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = {
          StringEquals = {
            "ssm:resourceTag/Name" = "${local.name}-app"
          }
        }
      },
      {
        # SendCommand authorises the document as a separate resource, and the
        # tag condition above cannot apply to it.
        Sid      = "TriggerDeployDocument"
        Effect   = "Allow"
        Action   = ["ssm:SendCommand"]
        Resource = "arn:aws:ssm:${var.region}::document/AWS-RunShellScript"
      },
      {
        # Lets the workflow discover the current instance by tag instead of
        # depending on an ID pinned in a repository secret.
        Sid      = "DiscoverInstance"
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances"]
        Resource = "*"
      },
      {
        Sid      = "WatchDeploy"
        Effect   = "Allow"
        Action   = ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"]
        Resource = "*"
      },
    ]
  })
}
