output "application_url" {
  description = "The live application. This is the URL to submit."
  value       = "https://${aws_cloudfront_distribution.main.domain_name}"
}

output "oauth_redirect_uri" {
  description = <<-EOT
    Add this EXACTLY to Google Cloud Console →
    APIs & Services → Credentials → your OAuth client → Authorised redirect URIs.
    Login fails with redirect_uri_mismatch until you do.
  EOT
  value       = "https://${aws_cloudfront_distribution.main.domain_name}/api/auth/google/callback"
}

output "github_actions_role_arn" {
  description = "Set as the AWS_ROLE_ARN secret in the GitHub repository."
  value       = aws_iam_role.github_actions.arn
}

output "ecr_repository_url" {
  description = "Set as the ECR_REPOSITORY secret in the GitHub repository."
  value       = aws_ecr_repository.api.repository_url
}

output "s3_frontend_bucket" {
  description = "Set as the S3_BUCKET secret in the GitHub repository."
  value       = aws_s3_bucket.frontend.id
}

output "cloudfront_distribution_id" {
  description = "Set as the CLOUDFRONT_DISTRIBUTION_ID secret in the GitHub repository."
  value       = aws_cloudfront_distribution.main.id
}

output "instance_id" {
  description = "Set as the EC2_INSTANCE_ID secret in the GitHub repository."
  value       = aws_instance.app.id
}

output "instance_public_ip" {
  description = "Elastic IP of the application host (origin only — not user-facing)."
  value       = aws_eip.app.public_ip
}

output "ssm_session_command" {
  description = "Open a shell on the instance without SSH or a key pair."
  value       = "aws ssm start-session --target ${aws_instance.app.id} --region ${var.region}"
}

output "github_secrets_to_set" {
  description = "Copy-paste summary of everything the CI pipeline needs."
  value = {
    AWS_ROLE_ARN               = aws_iam_role.github_actions.arn
    AWS_REGION                 = var.region
    ECR_REPOSITORY             = aws_ecr_repository.api.repository_url
    S3_BUCKET                  = aws_s3_bucket.frontend.id
    CLOUDFRONT_DISTRIBUTION_ID = aws_cloudfront_distribution.main.id
    EC2_INSTANCE_ID            = aws_instance.app.id
  }
}
