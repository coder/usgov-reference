resource "aws_db_subnet_group" "this" {
  name       = "${var.cluster_name}-db"
  subnet_ids = aws_subnet.private[*].id
  tags       = { Name = "${var.cluster_name}-db" }
}

resource "aws_security_group" "rds" {
  name        = "${var.cluster_name}-rds"
  description = "Postgres access from within the VPC"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "PostgreSQL from within the VPC (EKS nodes/pods)"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.cluster_name}-rds" }
}

resource "random_password" "db" {
  length  = 28
  special = false
}

# Multi-AZ instance (1 standby in another AZ). Multi-AZ DB *clusters* are not
# supported in GovCloud; the standard Multi-AZ instance deployment is.
resource "aws_db_instance" "this" {
  identifier            = "${var.cluster_name}-pg"
  engine                = "postgres"
  engine_version        = var.postgres_version
  instance_class        = var.db_instance_class
  allocated_storage     = 50
  max_allocated_storage = 200
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = "coder"
  username = "dbadmin"
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  multi_az                = true
  publicly_accessible     = false
  backup_retention_period = 7
  skip_final_snapshot     = true
  deletion_protection     = false
  apply_immediately       = true

  tags = { Name = "${var.cluster_name}-pg" }
}

resource "aws_secretsmanager_secret" "db" {
  name = "${var.cluster_name}/rds/master"
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({
    username = aws_db_instance.this.username
    password = random_password.db.result
    host     = aws_db_instance.this.address
    port     = aws_db_instance.this.port
  })
}
