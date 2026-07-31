# API Gateway HTTP API — the public HTTPS front door.
#
# Every request lands here and is proxied verbatim to nginx on the instance.
# nginx then serves the static frontend and forwards /api/* to FastAPI, so
# the browser sees a single origin: cookies are first-party, there is no CORS,
# and Google OAuth gets the HTTPS redirect URI it requires.
#
# The execute-api domain comes with a valid certificate at no cost, which is
# what makes this a drop-in replacement for CloudFront here.

resource "aws_apigatewayv2_api" "main" {
  name          = "${local.name}-api"
  protocol_type = "HTTP"
  description   = "Public HTTPS entry point for the Sentellent equity analyst"

  # Payloads are proxied through untouched. Binary types are auto-detected
  # from Content-Type, which matters for the frontend's JS, CSS and fonts.
  disable_execute_api_endpoint = false
}

# API Gateway's origin fetches come from a large, changing pool of AWS
# addresses, so the security group cannot restrict them the way it could with
# CloudFront's managed prefix list. Instead the gateway injects a shared secret
# on every request and nginx rejects anything without it — so hitting the
# instance's IP directly gets a 403 even though port 80 is open.
resource "random_password" "origin_token" {
  length  = 48
  special = false
}

resource "aws_apigatewayv2_integration" "app" {
  api_id             = aws_apigatewayv2_api.main.id
  integration_type   = "HTTP_PROXY"
  integration_method = "ANY"
  # No {proxy} placeholder in the URI: the catch-all $default route defines no
  # path variables, so referencing one is rejected at create time. The path is
  # carried by the `overwrite:path` mapping below instead, which forwards the
  # request path verbatim.
  integration_uri        = "http://${aws_eip.app.public_ip}"
  payload_format_version = "1.0"
  timeout_milliseconds   = 30000

  # X-Forwarded-Proto is deliberately absent: API Gateway reserves the
  # x-forwarded-* headers and rejects any mapping that touches them. nginx
  # sets it on the hop to FastAPI instead, which is where it actually matters.
  request_parameters = {
    "overwrite:path"                  = "$request.path"
    "overwrite:header.X-Origin-Token" = random_password.origin_token.result
  }
}

# A single catch-all route: the gateway is a dumb pipe, and nginx owns routing.
resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.app.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.apigw.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      httpMethod     = "$context.httpMethod"
      path           = "$context.path"
      status         = "$context.status"
      responseLength = "$context.responseLength"
      responseTime   = "$context.responseLatency"
      error          = "$context.integrationErrorMessage"
    })
  }

  default_route_settings {
    # Generous enough for a review deployment, low enough that a runaway
    # client cannot turn into a surprise bill.
    throttling_burst_limit = 100
    throttling_rate_limit  = 50
  }
}

resource "aws_cloudwatch_log_group" "apigw" {
  name              = "/${local.name}/apigateway"
  retention_in_days = 14
}
