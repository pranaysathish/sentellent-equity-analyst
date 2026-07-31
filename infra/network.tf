# A minimal public-subnet VPC.
#
# There is deliberately no NAT Gateway: at ~$32/month it would cost four times
# the compute it serves. The instance sits in a public subnet with an Elastic
# IP and reaches the internet through the Internet Gateway, which is free.
# Inbound exposure is closed off by the security group below.

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${local.name}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-igw" }
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${local.name}-public-rt" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# CloudFront publishes the IP ranges its origin-fetchers use as a managed
# prefix list. Allowing only those means the instance cannot be reached
# directly on its public IP — every request has to arrive through CloudFront,
# which is also the only path that terminates TLS.
data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "app" {
  name        = "${local.name}-app"
  description = "Application host: CloudFront-only ingress, unrestricted egress"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "HTTP from CloudFront edge locations only"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront.id]
  }

  # No SSH rule on purpose. Shell access is via SSM Session Manager, which
  # needs no open port and no key pair to lose.
  egress {
    description = "Outbound to screener.in, RSS feeds, Gemini, ECR"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-app-sg" }
}
