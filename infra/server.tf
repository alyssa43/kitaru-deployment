data "aws_ssm_parameter" "ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  hostname    = var.domain != null ? var.domain : "${replace(aws_eip.server.public_ip, ".", "-")}.sslip.io"
  url         = "https://${local.hostname}"
  caddy_image = "caddy@sha256:c2aeffaeb80d5403e9c8d1d908b50e191aad204e16129f991ae357ada642094d" # 2.11.9
}

resource "aws_eip" "server" {
  domain = "vpc"
  tags   = { Name = "evalve" }
}

resource "aws_eip_association" "server" {
  instance_id   = aws_instance.server.id
  allocation_id = aws_eip.server.id
}

resource "aws_iam_role" "server" {
  name_prefix = "evalve-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.server.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "secrets" {
  role = aws_iam_role.server.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = [var.application_secret_arn, aws_db_instance.main.master_user_secret[0].secret_arn]
    }]
  })
}

resource "aws_iam_instance_profile" "server" {
  name_prefix = "evalve-"
  role        = aws_iam_role.server.name
}

resource "aws_instance" "server" {
  ami                         = data.aws_ssm_parameter.ami.value
  instance_type               = var.server_instance_type
  subnet_id                   = aws_subnet.server.id
  associate_public_ip_address = false
  vpc_security_group_ids      = [aws_security_group.server.id]
  iam_instance_profile        = aws_iam_instance_profile.server.name
  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/bootstrap.sh.tftpl", {
    config = base64encode(jsonencode({
      region                     = var.region
      application_secret_arn     = var.application_secret_arn
      application_secret_version = var.application_secret_version
      database_secret_arn        = aws_db_instance.main.master_user_secret[0].secret_arn
      database_host              = aws_db_instance.main.address
      hostname                   = local.hostname
    }))
    refresh_script = filebase64("${path.module}/refresh.py")
    hostname       = local.hostname
    public_ip      = aws_eip.server.public_ip
    kitaru_image   = var.server_image
    caddy_image    = local.caddy_image
  })

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
    encrypted   = true
  }

  tags = { Name = "evalve" }

  depends_on = [
    aws_route_table_association.server,
    aws_iam_role_policy.secrets,
    aws_iam_role_policy_attachment.ssm,
    aws_vpc_security_group_egress_rule.https,
    aws_vpc_security_group_egress_rule.postgres,
    aws_vpc_security_group_ingress_rule.postgres,
    aws_vpc_security_group_ingress_rule.web,
  ]
}
