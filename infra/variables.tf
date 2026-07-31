variable "project" {
  description = "Name prefix applied to every resource."
  type        = string
  default     = "sentellent"
}

variable "region" {
  description = "AWS region. Mumbai keeps latency low for Indian data sources."
  type        = string
  default     = "ap-south-1"
}

variable "instance_type" {
  description = <<-EOT
    EC2 instance type for the application host.

    t3.micro (2 vCPU / 1 GiB) is the default because it is the cheapest type
    that comfortably runs the API and Postgres together, and it is the
    free-tier-eligible type on accounts that still have the 12-month allowance.
    A 2 GiB swap file is provisioned to absorb ingestion spikes. Move to
    t3.small if you see the API being OOM-killed under heavy ingestion.
  EOT
  type        = string
  default     = "t3.micro"
}

variable "root_volume_gb" {
  description = "Root EBS volume size. Holds Docker images plus the Postgres data directory."
  type        = number
  default     = 20
}

variable "github_repository" {
  description = "GitHub repo allowed to deploy, as owner/name."
  type        = string
}

variable "google_client_id" {
  description = "Google OAuth client ID."
  type        = string
  sensitive   = true
}

variable "google_client_secret" {
  description = "Google OAuth client secret."
  type        = string
  sensitive   = true
}

variable "google_api_key" {
  description = "Gemini API key, used for both chat and embeddings."
  type        = string
  sensitive   = true
  default     = ""
}

variable "llm_provider" {
  description = "Which provider serves chat completions."
  type        = string
  default     = "gemini"

  validation {
    condition     = contains(["gemini", "openai", "anthropic", "echo"], var.llm_provider)
    error_message = "llm_provider must be one of: gemini, openai, anthropic, echo."
  }
}

variable "embedding_provider" {
  description = "Which provider serves embeddings."
  type        = string
  default     = "gemini"

  validation {
    condition     = contains(["gemini", "openai", "hash"], var.embedding_provider)
    error_message = "embedding_provider must be one of: gemini, openai, hash."
  }
}

variable "llm_model" {
  description = <<-EOT
    Chat model id. Blank uses the provider default from app/llm.py.

    Pinned explicitly because availability and quota vary per model, not
    just per key. On the free tier each model carries its own daily request
    allowance — gemini-2.5-flash permits 20 requests per project per day,
    which a single afternoon of testing exhausts. Switching models buys a
    fresh allowance; enabling billing removes the ceiling.
  EOT
  type        = string
  default     = "gemini-flash-latest"
}

variable "embedding_model" {
  description = <<-EOT
    Embedding model id. Blank uses the provider default.

    gemini-embedding-001 replaces the retired text-embedding-004, which newer
    keys cannot resolve at all.
  EOT
  type        = string
  default     = "gemini-embedding-001"
}

variable "db_password" {
  description = "Postgres password. Leave blank to have Terraform generate one."
  type        = string
  sensitive   = true
  default     = ""
}

variable "alert_email" {
  description = "Address that receives the billing alarm. Requires confirming the SNS subscription email."
  type        = string
}

variable "monthly_budget_usd" {
  description = "Spend threshold that triggers the billing alarm, in USD."
  type        = number
  default     = 15
}

variable "enable_cloudfront" {
  description = <<-EOT
    Serve the app through CloudFront instead of API Gateway.

    CloudFront is the better front door — edge caching, cheaper at volume —
    but AWS blocks distribution creation on unverified new accounts with
    "your account must be verified before you can add new CloudFront
    resources", which only Support can lift.

    API Gateway provides the same trusted HTTPS endpoint with no such gate,
    so it is the default. Flip this to true once Support verifies the
    account; `public_base_url` follows automatically.
  EOT
  type        = bool
  default     = false
}

variable "news_refresh_cron" {
  description = "Schedule for the news/sentiment refresh job (UTC). Default: every 6 hours."
  type        = string
  default     = "0 */6 * * *"
}
