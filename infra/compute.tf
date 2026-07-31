# The application host: one instance running the API and Postgres as
# containers, fronted by CloudFront.

data "aws_ssm_parameter" "al2023_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

# --------------------------------------------------------------------------- #
# Secrets — SSM Parameter Store rather than Secrets Manager.
# Functionally equivalent for this use and free, where Secrets Manager bills
# $0.40 per secret per month. Values are SecureString (KMS-encrypted at rest).
# --------------------------------------------------------------------------- #
resource "aws_ssm_parameter" "app_env" {
  for_each = {
    DATABASE_URL           = local.database_url
    POSTGRES_PASSWORD      = local.db_password
    SESSION_SECRET         = random_password.session_secret.result
    INTERNAL_REFRESH_TOKEN = random_password.internal_token.result
    GOOGLE_CLIENT_ID       = var.google_client_id
    GOOGLE_CLIENT_SECRET   = var.google_client_secret
    GOOGLE_API_KEY         = var.google_api_key
  }

  name  = "/${local.name}/${each.key}"
  type  = "SecureString"
  value = each.value

  tags = { Name = "${local.name}-${lower(each.key)}" }
}

# Non-secret configuration, kept separate so it is readable in the console.
resource "aws_ssm_parameter" "app_config" {
  for_each = {
    ENVIRONMENT        = "prod"
    LLM_PROVIDER       = var.llm_provider
    EMBEDDING_PROVIDER = var.embedding_provider
    LLM_MODEL          = var.llm_model
    EMBEDDING_MODEL    = var.embedding_model
    FRONTEND_BASE_URL  = "https://${aws_cloudfront_distribution.main.domain_name}"
    OAUTH_REDIRECT_URI = "https://${aws_cloudfront_distribution.main.domain_name}/api/auth/google/callback"
  }

  name  = "/${local.name}/${each.key}"
  type  = "String"
  value = each.value
}

# --------------------------------------------------------------------------- #
# Instance role
# --------------------------------------------------------------------------- #
resource "aws_iam_role" "app" {
  name = "${local.name}-app-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Enables Session Manager shell access and Run Command deploys.
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "app" {
  name = "${local.name}-app-policy"
  role = aws_iam_role.app.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "PullApplicationImage"
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
        ]
        Resource = "*"
      },
      {
        Sid      = "ReadOwnConfiguration"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
        Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/${local.name}/*"
      },
      {
        Sid      = "WriteLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = "${aws_cloudwatch_log_group.app.arn}:*"
      },
    ]
  })
}

resource "aws_iam_instance_profile" "app" {
  name = "${local.name}-app-profile"
  role = aws_iam_role.app.name
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/${local.name}/app"
  retention_in_days = 14
}

# --------------------------------------------------------------------------- #
# Instance
# --------------------------------------------------------------------------- #
resource "aws_instance" "app" {
  ami                    = data.aws_ssm_parameter.al2023_ami.value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.app.name

  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/user_data.sh", {
    aws_region        = var.region
    project           = local.name
    ecr_repository    = aws_ecr_repository.api.repository_url
    log_group         = aws_cloudwatch_log_group.app.name
    news_refresh_cron = var.news_refresh_cron
  })

  # Secrets must exist before the host boots and reads them. Only `app_env` is
  # listed: `app_config` carries the CloudFront URL, and CloudFront depends on
  # this instance's address — depending on it here would be a cycle. The
  # deploy script re-reads Parameter Store on every deploy, so the config
  # params land on the first CI run.
  depends_on = [aws_ssm_parameter.app_env]

  root_block_device {
    volume_size           = var.root_volume_gb
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  metadata_options {
    http_tokens                 = "required" # IMDSv2 only
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 2 # containers need one extra hop
  }

  tags = { Name = "${local.name}-app" }
}

# A static address so the CloudFront origin does not change when the instance
# is stopped and started.
resource "aws_eip" "app" {
  instance = aws_instance.app.id
  domain   = "vpc"
  tags     = { Name = "${local.name}-eip" }
}
