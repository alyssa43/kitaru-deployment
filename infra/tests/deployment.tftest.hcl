mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1b", "us-east-1a"] }
  }

  mock_data "aws_ssm_parameter" {
    defaults = { value = "ami-0123456789abcdef0" }
  }

  mock_resource "aws_eip" {
    defaults = { public_ip = "203.0.113.10" }
  }

  mock_resource "aws_db_instance" {
    defaults = {
      address = "evalve.example.us-east-1.rds.amazonaws.com"
      master_user_secret = [{
        secret_arn    = "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds-test"
        kms_key_id    = "test"
        secret_status = "active"
      }]
    }
  }
}

variables {
  account_id              = "123456789012"
  region                  = "us-east-1"
  application_secret_arn  = "arn:aws:secretsmanager:us-east-1:123456789012:secret:evalve-test"
  server_instance_type    = "t3.small"
  database_instance_class = "db.t4g.micro"
}

run "dedicated_network" {
  command = apply

  assert {
    condition = (
      aws_subnet.server.vpc_id == aws_vpc.main.id &&
      aws_route_table_association.server.subnet_id == aws_subnet.server.id &&
      aws_instance.server.subnet_id == aws_subnet.server.id
    )
    error_message = "Only the server subnet should use the public route table."
  }

  assert {
    condition = (
      length(aws_subnet.database) == 2 &&
      length(toset(aws_subnet.database[*].availability_zone)) == 2 &&
      aws_db_subnet_group.main.subnet_ids == toset(aws_subnet.database[*].id) &&
      alltrue([for subnet in aws_subnet.database : subnet.vpc_id == aws_vpc.main.id && !subnet.map_public_ip_on_launch])
    )
    error_message = "The DB subnet group must span two private subnets in different Availability Zones."
  }

  assert {
    condition = (
      aws_db_instance.main.availability_zone == aws_subnet.server.availability_zone &&
      !aws_db_instance.main.multi_az && !aws_db_instance.main.publicly_accessible &&
      aws_db_instance.main.storage_encrypted && aws_db_instance.main.manage_master_user_password &&
      aws_db_instance.main.backup_retention_period == 7 && aws_db_instance.main.deletion_protection &&
      !aws_db_instance.main.skip_final_snapshot
    )
    error_message = "The database must be private, single-AZ, encrypted, and protected against accidental deletion."
  }

  assert {
    condition = (
      toset([for rule in aws_vpc_security_group_ingress_rule.web : rule.from_port]) == toset([80, 443]) &&
      aws_vpc_security_group_ingress_rule.postgres.referenced_security_group_id == aws_security_group.server.id &&
      aws_vpc_security_group_ingress_rule.postgres.cidr_ipv4 == null &&
      aws_vpc_security_group_ingress_rule.postgres.from_port == 5432
    )
    error_message = "Public ingress must be limited to HTTP and HTTPS, with PostgreSQL reachable only from EC2."
  }

  assert {
    condition = (
      !aws_instance.server.associate_public_ip_address &&
      aws_eip_association.server.allocation_id == aws_eip.server.id &&
      aws_eip_association.server.instance_id == aws_instance.server.id &&
      aws_instance.server.metadata_options[0].http_tokens == "required" &&
      aws_instance.server.root_block_device[0].encrypted
    )
    error_message = "The instance must use its Elastic IP, IMDSv2, and encrypted EBS storage."
  }

  assert {
    condition = toset(jsondecode(aws_iam_role_policy.secrets.policy).Statement[0].Resource) == toset([
      var.application_secret_arn,
      aws_db_instance.main.master_user_secret[0].secret_arn,
    ])
    error_message = "The server role must only read the two Evalve secrets."
  }

  assert {
    condition = (
      output.url == "https://203-0-113-10.sslip.io" &&
      strcontains(aws_instance.server.user_data, "203-0-113-10.sslip.io {") &&
      strcontains(aws_instance.server.user_data, "reverse_proxy 127.0.0.1:8000")
    )
    error_message = "Caddy must serve the Elastic IP's sslip.io hostname and proxy to Kitaru on loopback."
  }
}

run "custom_domain" {
  command = apply

  variables {
    domain = "evalve.example.com"
  }

  assert {
    condition = (
      output.url == "https://evalve.example.com" && output.public_ip == "203.0.113.10" &&
      strcontains(aws_instance.server.user_data, "evalve.example.com {") &&
      !strcontains(aws_instance.server.user_data, "203-0-113-10.sslip.io {")
    )
    error_message = "A custom hostname should serve the same Elastic IP and replace the default hostname."
  }
}

run "free_plan_uses_one_day_of_automated_backups" {
  command = apply

  variables {
    backup_retention_days = 1
  }

  assert {
    condition     = aws_db_instance.main.backup_retention_period == 1 && output.deployment.backup_retention_days == 1
    error_message = "The Free plan override must reach RDS and be recorded in deployment state."
  }
}

run "reject_foreign_secret" {
  command = plan

  variables {
    application_secret_arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:other-account"
  }

  expect_failures = [var.application_secret_arn]
}

run "restore_keeps_database_private_and_pins_application_secret" {
  command = apply

  variables {
    snapshot_id                = "evalve-20261110170000-123456789abc"
    database_engine_version    = "16.12"
    application_secret_version = "12345678-1234-1234-1234-123456789abc"
  }

  assert {
    condition = (
      aws_db_instance.main.snapshot_identifier == var.snapshot_id &&
      aws_db_instance.main.engine_version == var.database_engine_version &&
      aws_db_instance.main.manage_master_user_password &&
      aws_db_instance.main.deletion_protection &&
      !aws_db_instance.main.publicly_accessible &&
      output.deployment.application_secret_version == var.application_secret_version
    )
    error_message = "Restoration must use the saved snapshot and credential version, with a private, protected RDS instance."
  }
}
