variable "account_id" {
  type        = string
  description = "Expected AWS account ID."

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Provide a 12-digit AWS account ID."
  }
}

variable "region" {
  type        = string
  description = "AWS region for the deployment."
}

variable "server_instance_type" {
  type        = string
  description = "EC2 instance type compatible with the x86-64 Amazon Linux AMI."
}

variable "database_instance_class" {
  type        = string
  description = "RDS instance class for the single-AZ PostgreSQL database."
}

variable "backup_retention_days" {
  type        = number
  default     = 7
  nullable    = false
  description = "Days of RDS automated backup retention."

  validation {
    condition     = var.backup_retention_days >= 1 && var.backup_retention_days <= 35 && floor(var.backup_retention_days) == var.backup_retention_days
    error_message = "Backup retention must be a whole number between 1 and 35 days."
  }
}

variable "application_secret_arn" {
  type        = string
  description = "Secrets Manager ARN for Kitaru's signing key, encryption key, and bootstrap password."

  validation {
    condition     = startswith(var.application_secret_arn, "arn:aws:secretsmanager:${var.region}:${var.account_id}:secret:")
    error_message = "The application secret must belong to the deployment account and region."
  }
}

variable "domain" {
  type        = string
  default     = null
  description = "Optional custom DNS name with an A record pointing to the Elastic IP."

  validation {
    condition     = var.domain == null || can(regex("^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,63}$", var.domain))
    error_message = "Use a lowercase DNS hostname, or leave domain unset to use sslip.io."
  }
}

variable "snapshot_id" {
  type    = string
  default = null
}

variable "database_engine_version" {
  type    = string
  default = "16"
}

variable "server_image" {
  type    = string
  default = "zenmldocker/kitaru-server@sha256:05ea00482c0adf4609a34d2c95575a6530ce5d60f6cea0e79716336d9e251d47" # 0.27.6

  validation {
    condition     = can(regex("^zenmldocker/kitaru-server@sha256:[a-f0-9]{64}$", var.server_image))
    error_message = "Use an immutable Kitaru server image digest."
  }
}

variable "application_secret_version" {
  type    = string
  default = null
}

variable "verified_shutdown" {
  type    = bool
  default = false
}
