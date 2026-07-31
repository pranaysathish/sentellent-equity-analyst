# S3 hosts the exported Next.js site; CloudFront is the single HTTPS front door
# for both the site and the API.
#
# Serving both through one distribution means the browser sees one origin, so
# the session cookie is first-party, there is no CORS preflight, and Google
# OAuth gets the HTTPS redirect URI it insists on — without buying a domain or
# a certificate.

resource "aws_s3_bucket" "frontend" {
  bucket        = "${local.name}-frontend-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # this is a review deployment; teardown should be one command
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

# Origin Access Control keeps the bucket private: only this distribution can
# read it, so there is no public bucket to misconfigure.
resource "aws_cloudfront_origin_access_control" "frontend" {
  count = var.enable_cloudfront ? 1 : 0

  name                              = "${local.name}-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_s3_bucket_policy" "frontend" {
  count = var.enable_cloudfront ? 1 : 0

  bucket = aws_s3_bucket.frontend.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCloudFrontRead"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.frontend.arn}/*"
      Condition = {
        StringEquals = {
          "AWS:SourceArn" = aws_cloudfront_distribution.main[0].arn
        }
      }
    }]
  })
}

# Next.js static export writes `dashboard/index.html`, but a browser asks for
# `/dashboard/`. S3 only resolves index documents for website endpoints, which
# OAC does not use — so the rewrite happens at the edge instead.
resource "aws_cloudfront_function" "rewrite_index" {
  count = var.enable_cloudfront ? 1 : 0

  name    = "${local.name}-rewrite-index"
  runtime = "cloudfront-js-2.0"
  comment = "Map directory-style paths to their index.html object"
  publish = true

  code = <<-JS
    function handler(event) {
      var request = event.request;
      var uri = request.uri;

      if (uri.endsWith('/')) {
        request.uri = uri + 'index.html';
      } else if (!uri.includes('.')) {
        request.uri = uri + '/index.html';
      }
      return request;
    }
  JS
}

data "aws_cloudfront_cache_policy" "optimized" {
  count = var.enable_cloudfront ? 1 : 0

  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_cache_policy" "disabled" {
  count = var.enable_cloudfront ? 1 : 0

  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer" {
  count = var.enable_cloudfront ? 1 : 0

  name = "Managed-AllViewerExceptHostHeader"
}

resource "aws_cloudfront_distribution" "main" {
  count = var.enable_cloudfront ? 1 : 0

  enabled             = true
  comment             = "${local.name} — SPA and API"
  default_root_object = "index.html"
  price_class         = "PriceClass_100" # cheapest tier; ample for a review deployment

  origin {
    origin_id                = "s3-frontend"
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend[0].id
  }

  origin {
    origin_id   = "ec2-api"
    domain_name = aws_eip.app.public_dns

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only" # TLS terminates at CloudFront
      origin_ssl_protocols   = ["TLSv1.2"]
      origin_read_timeout    = 60
    }
  }

  default_cache_behavior {
    target_origin_id       = "s3-frontend"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = data.aws_cloudfront_cache_policy.optimized[0].id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.rewrite_index[0].arn
    }
  }

  ordered_cache_behavior {
    path_pattern           = "/api/*"
    target_origin_id       = "ec2-api"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    # API responses are per-user and must never be cached; the origin request
    # policy forwards cookies, headers and query strings so sessions work.
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled[0].id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer[0].id
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    cloudfront_default_certificate = true # gives HTTPS on *.cloudfront.net free
  }

  tags = { Name = "${local.name}-cdn" }
}
