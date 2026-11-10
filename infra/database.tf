resource "aws_db_subnet_group" "main" {
  name_prefix = "evalve-"
  subnet_ids  = aws_subnet.database[*].id
}

resource "aws_db_instance" "main" {
  identifier                  = "evalve"
  engine                      = "postgres"
  engine_version              = var.database_engine_version
  instance_class              = var.database_instance_class
  allocated_storage           = var.snapshot_id == null ? 20 : null
  storage_type                = "gp3"
  storage_encrypted           = true
  db_name                     = var.snapshot_id == null ? "kitaru" : null
  username                    = var.snapshot_id == null ? "evalve_admin" : null
  snapshot_identifier         = var.snapshot_id
  manage_master_user_password = true
  availability_zone           = aws_subnet.server.availability_zone
  db_subnet_group_name        = aws_db_subnet_group.main.name
  vpc_security_group_ids      = [aws_security_group.database.id]
  publicly_accessible         = false
  multi_az                    = false
  backup_retention_period     = var.backup_retention_days
  copy_tags_to_snapshot       = true
  deletion_protection         = !var.verified_shutdown
  skip_final_snapshot         = var.verified_shutdown
}
