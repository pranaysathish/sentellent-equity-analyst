# Managed PostgreSQL with pgvector — the vector store.
#
# Previously Postgres ran as a container beside the API on the instance. That
# worked, but coupled the data to the host: any change to user_data replaces
# the instance and takes the database with it. Moving it to RDS makes the
# instance disposable, which is what lets the deployment be rebuilt from
# scratch at any time without data loss.
#
# It is also what the challenge brief names directly ("pgvector on RDS").

resource "aws_subnet" "private_b" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.2.0/24"
  availability_zone = data.aws_availability_zones.available.names[1]

  tags = { Name = "${local.name}-private-b" }
}

# A DB subnet group needs subnets in at least two availability zones, even for
# a single-AZ instance — RDS requires the option to fail over to exist.
resource "aws_db_subnet_group" "main" {
  name       = "${local.name}-db"
  subnet_ids = [aws_subnet.public.id, aws_subnet.private_b.id]

  tags = { Name = "${local.name}-db-subnet-group" }
}

resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "PostgreSQL, reachable only from the application instance"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "PostgreSQL from the application security group"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }

  tags = { Name = "${local.name}-db-sg" }
}

resource "aws_db_parameter_group" "main" {
  name   = "${local.name}-pg16"
  family = "postgres16"

  # pgvector ships with RDS PostgreSQL but the extension still has to be
  # created per database; the migration does that. Preloading nothing here
  # keeps the parameter group minimal.
  parameter {
    name  = "log_min_duration_statement"
    value = "1000" # log anything slower than a second
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "main" {
  identifier = "${local.name}-db"
  engine     = "postgres"

  # Pinned to a version this region actually offers. RDS exposes a different
  # set of minor versions per region and rejects anything outside it, so the
  # value comes from `aws rds describe-db-engine-versions` for ap-south-1
  # rather than from the upstream PostgreSQL release list.
  engine_version = "16.14"

  # Smallest current-generation instance. Graviton, so cheaper than t3 for the
  # same memory, and comfortably ahead of what this workload needs.
  instance_class = "db.t4g.micro"

  allocated_storage     = 20
  max_allocated_storage = 50 # autoscale rather than run out mid-ingestion
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = "sentellent"
  username = "sentellent"
  password = local.db_password
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.main.name

  # Not reachable from the internet; only the application security group can
  # open a connection.
  publicly_accessible = false
  multi_az            = false # single-AZ keeps this within the free credits

  backup_retention_period = 1
  backup_window           = "18:00-19:00" # ~23:30 IST, outside market hours
  maintenance_window      = "sun:19:30-sun:20:30"

  # This is a review deployment that should tear down in one command.
  skip_final_snapshot = true
  deletion_protection = false

  auto_minor_version_upgrade = true
  apply_immediately          = true

  performance_insights_enabled = false # not free on t4g.micro
  copy_tags_to_snapshot        = true

  tags = { Name = "${local.name}-db" }
}
