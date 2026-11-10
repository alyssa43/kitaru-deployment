output "url" { value = local.url }
output "public_ip" { value = aws_eip.server.public_ip }
output "instance_id" { value = aws_instance.server.id }
output "database_id" { value = aws_db_instance.main.identifier }

output "deployment" {
  description = "Non-secret settings for subsequent Evalve operations."
  value = {
    domain                     = var.domain
    server_instance_type       = var.server_instance_type
    database_instance_class    = var.database_instance_class
    backup_retention_days      = var.backup_retention_days
    application_secret_arn     = var.application_secret_arn
    application_secret_version = var.application_secret_version
    database_engine_version    = var.database_engine_version
    server_image               = var.server_image
    snapshot_id                = var.snapshot_id
  }
}
