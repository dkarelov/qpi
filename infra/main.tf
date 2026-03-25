data "yandex_vpc_network" "main" {
  folder_id = var.folder_id
  name      = var.network_name
}

data "yandex_vpc_subnet" "main" {
  folder_id = var.folder_id
  name      = var.subnet_name
}

resource "random_password" "db_password" {
  length  = 24
  special = false
}

resource "yandex_iam_service_account" "bot_vm" {
  folder_id   = var.folder_id
  name        = "${var.project}-bot-vm-sa"
  description = "Service account for bot VM instance group."
}

resource "yandex_resourcemanager_folder_iam_member" "bot_vm_logging_writer" {
  folder_id = var.folder_id
  role      = "logging.writer"
  member    = "serviceAccount:${yandex_iam_service_account.bot_vm.id}"
}

resource "yandex_resourcemanager_folder_iam_member" "bot_vm_editor" {
  folder_id   = var.folder_id
  role        = "editor"
  member      = "serviceAccount:${yandex_iam_service_account.bot_vm.id}"
  sleep_after = 15
}

resource "yandex_logging_group" "main" {
  folder_id = var.folder_id
  name      = "${var.project}-prod-logs"
}

resource "yandex_vpc_security_group" "bot" {
  folder_id  = var.folder_id
  name       = "${var.project}-sg-bot"
  network_id = data.yandex_vpc_network.main.id

  ingress {
    protocol       = "TCP"
    description    = "Telegram webhook HTTPS"
    port           = var.bot_webhook_port
    v4_cidr_blocks = ["0.0.0.0/0"]
  }

  dynamic "ingress" {
    for_each = toset(var.admin_ipv4_cidrs)
    content {
      protocol       = "TCP"
      description    = "OS Login SSH"
      port           = 22
      v4_cidr_blocks = [ingress.value]
    }
  }

  dynamic "ingress" {
    for_each = toset(var.admin_ipv4_cidrs)
    content {
      protocol       = "TCP"
      description    = "Bot health endpoint"
      port           = var.bot_health_port
      v4_cidr_blocks = [ingress.value]
    }
  }

  egress {
    protocol       = "ANY"
    description    = "Allow all egress"
    v4_cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "yandex_vpc_security_group" "db" {
  folder_id  = var.folder_id
  name       = "${var.project}-sg-db"
  network_id = data.yandex_vpc_network.main.id

  ingress {
    protocol          = "TCP"
    description       = "PostgreSQL from bot SG"
    port              = 5432
    security_group_id = yandex_vpc_security_group.bot.id
  }

  ingress {
    protocol          = "TCP"
    description       = "SSH from private runner SG"
    port              = 22
    security_group_id = yandex_vpc_security_group.runner.id
  }

  ingress {
    protocol       = "TCP"
    description    = "PostgreSQL from VPC subnets (Cloud Functions connectivity)"
    port           = 5432
    v4_cidr_blocks = ["0.0.0.0/0"]
  }

  dynamic "ingress" {
    for_each = toset(var.admin_ipv4_cidrs)
    content {
      protocol       = "TCP"
      description    = "OS Login SSH"
      port           = 22
      v4_cidr_blocks = [ingress.value]
    }
  }

  egress {
    protocol       = "ANY"
    description    = "Allow all egress"
    v4_cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "yandex_vpc_security_group" "runner" {
  folder_id  = var.folder_id
  name       = "${var.project}-sg-runner"
  network_id = data.yandex_vpc_network.main.id

  dynamic "ingress" {
    for_each = toset(var.runner_bootstrap_ipv4_cidrs)
    content {
      protocol       = "TCP"
      description    = "Runner SSH bootstrap"
      port           = 22
      v4_cidr_blocks = [ingress.value]
    }
  }

  egress {
    protocol       = "ANY"
    description    = "Allow all egress"
    v4_cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "yandex_vpc_address" "bot_public_ip" {
  folder_id = var.folder_id
  name      = "${var.project}-bot-ip"

  external_ipv4_address {
    zone_id = var.zone
  }
}

resource "yandex_vpc_gateway" "nat" {
  folder_id = var.folder_id
  name      = "${var.project}-nat-gw"

  shared_egress_gateway {}
}

resource "yandex_vpc_route_table" "private" {
  folder_id  = var.folder_id
  name       = "${var.project}-rt-private"
  network_id = data.yandex_vpc_network.main.id

  static_route {
    destination_prefix = "0.0.0.0/0"
    gateway_id         = yandex_vpc_gateway.nat.id
  }
}

resource "yandex_vpc_subnet" "private" {
  folder_id      = var.folder_id
  name           = "${var.project}-private-${var.zone}"
  zone           = var.zone
  network_id     = data.yandex_vpc_network.main.id
  v4_cidr_blocks = [var.private_subnet_cidr]
  route_table_id = yandex_vpc_route_table.private.id
}

locals {
  bot_cloud_init = templatefile("${path.module}/cloud-init/bot.yaml.tftpl", {
    folder_id     = var.folder_id
    log_group_id  = yandex_logging_group.main.id
    bot_app_env   = var.bot_app_env
    bot_log_level = var.bot_log_level
    bot_database_url = format(
      "postgresql://%s:%s@%s:5432/%s",
      var.db_user,
      random_password.db_password.result,
      yandex_compute_instance.db.network_interface[0].ip_address,
      var.db_name,
    )
    bot_webhook_base_url = format(
      "https://%s:%d",
      yandex_vpc_address.bot_public_ip.external_ipv4_address[0].address,
      var.bot_webhook_port,
    )
    bot_public_ip            = yandex_vpc_address.bot_public_ip.external_ipv4_address[0].address
    bot_webhook_port         = var.bot_webhook_port
    bot_webhook_secret_token = var.bot_webhook_secret_token
    bot_health_port          = var.bot_health_port
    telegram_bot_username    = var.telegram_bot_username
    token_cipher_key         = var.cf_token_cipher_key
    bot_admin_telegram_ids_csv = join(
      ",",
      [for id in var.bot_admin_telegram_ids : tostring(id)]
    )
    seller_collateral_shard_key         = var.seller_collateral_shard_key
    seller_collateral_shard_address     = var.seller_collateral_shard_address
    seller_collateral_shard_chain       = var.seller_collateral_shard_chain
    seller_collateral_shard_asset       = var.seller_collateral_shard_asset
    seller_collateral_invoice_ttl_hours = var.seller_collateral_invoice_ttl_hours
  })

  bot_ssh_keys_metadata = join("\n", [
    for key in var.bot_ssh_public_keys : "ubuntu:${trimspace(key)}"
  ])

  db_ssh_keys_metadata = join("\n", [
    for key in var.db_ssh_public_keys : "ubuntu:${trimspace(key)}"
  ])

  runner_ssh_keys_metadata = join("\n", [
    for key in var.runner_ssh_public_keys : "ubuntu:${trimspace(key)}"
  ])

  db_cloud_init = templatefile("${path.module}/cloud-init/db.yaml.tftpl", {
    postgres_version         = var.postgres_version
    db_name                  = var.db_name
    db_user                  = var.db_user
    db_password              = random_password.db_password.result
    serverless_postgres_cidr = var.serverless_postgres_cidr
  })

  runner_cloud_init = templatefile("${path.module}/cloud-init/runner.yaml.tftpl", {
    folder_id    = var.folder_id
    log_group_id = yandex_logging_group.main.id
  })

  operator_ssh_private_key_path = pathexpand(var.operator_ssh_private_key_path)
}

resource "yandex_compute_instance_group" "bot" {
  folder_id           = var.folder_id
  name                = "${var.project}-bot-ig"
  service_account_id  = yandex_iam_service_account.bot_vm.id
  deletion_protection = var.deletion_protection
  depends_on = [
    yandex_resourcemanager_folder_iam_member.bot_vm_editor
  ]

  instance_template {
    name        = "${var.project}-bot-{instance.index}"
    platform_id = var.bot_platform_id

    resources {
      cores         = var.bot_cores
      memory        = var.bot_memory_gb
      core_fraction = 100
    }

    boot_disk {
      mode = "READ_WRITE"
      initialize_params {
        image_id = var.ubuntu_2404_lts_image_id
        type     = "network-ssd"
        size     = var.bot_disk_gb
      }
    }

    network_interface {
      network_id         = data.yandex_vpc_network.main.id
      subnet_ids         = [data.yandex_vpc_subnet.main.id]
      nat                = true
      nat_ip_address     = yandex_vpc_address.bot_public_ip.external_ipv4_address[0].address
      security_group_ids = [yandex_vpc_security_group.bot.id]
    }

    scheduling_policy {
      preemptible = true
    }

    service_account_id = yandex_iam_service_account.bot_vm.id

    metadata = merge(
      {
        enable-oslogin     = "false"
        serial-port-enable = "1"
        user-data          = local.bot_cloud_init
      },
      length(var.bot_ssh_public_keys) > 0 ? {
        "ssh-keys" = local.bot_ssh_keys_metadata
      } : {}
    )
  }

  scale_policy {
    fixed_scale {
      size = 1
    }
  }

  allocation_policy {
    zones = [var.zone]
  }

  deploy_policy {
    max_unavailable = 1
    max_expansion   = 0
    max_creating    = 1
    max_deleting    = 1
  }

  health_check {
    interval            = 30
    timeout             = 10
    healthy_threshold   = 2
    unhealthy_threshold = 3
    tcp_options {
      port = 22
    }
  }
}

resource "yandex_compute_instance" "db" {
  folder_id                 = var.folder_id
  name                      = "${var.project}-db"
  zone                      = var.zone
  platform_id               = var.db_platform_id
  allow_stopping_for_update = true

  resources {
    cores         = var.db_cores
    memory        = var.db_memory_gb
    core_fraction = 100
  }

  boot_disk {
    initialize_params {
      image_id = var.ubuntu_2404_lts_image_id
      type     = "network-ssd"
      size     = var.db_disk_gb
    }
  }

  network_interface {
    subnet_id          = yandex_vpc_subnet.private.id
    nat                = false
    security_group_ids = [yandex_vpc_security_group.db.id]
  }

  metadata = merge(
    {
      enable-oslogin     = "false"
      serial-port-enable = "1"
      user-data          = local.db_cloud_init
    },
    length(var.db_ssh_public_keys) > 0 ? {
      "ssh-keys" = local.db_ssh_keys_metadata
    } : {}
  )

  labels = {
    project = var.project
    role    = "db"
  }
}

resource "yandex_compute_instance" "runner" {
  folder_id                 = var.folder_id
  name                      = "${var.project}-private-runner"
  zone                      = var.zone
  platform_id               = var.runner_platform_id
  allow_stopping_for_update = true

  resources {
    cores         = var.runner_cores
    memory        = var.runner_memory_gb
    core_fraction = 100
  }

  boot_disk {
    initialize_params {
      image_id = var.ubuntu_2404_lts_image_id
      type     = "network-ssd"
      size     = var.runner_disk_gb
    }
  }

  network_interface {
    subnet_id          = data.yandex_vpc_subnet.main.id
    nat                = true
    security_group_ids = [yandex_vpc_security_group.runner.id]
  }

  scheduling_policy {
    preemptible = true
  }

  metadata = merge(
    {
      enable-oslogin     = "false"
      serial-port-enable = "1"
      user-data          = local.runner_cloud_init
    },
    length(var.runner_ssh_public_keys) > 0 ? {
      "ssh-keys" = local.runner_ssh_keys_metadata
    } : {}
  )

  labels = {
    project = var.project
    role    = "private-runner"
  }
}

resource "terraform_data" "db_pg_hba_serverless" {
  triggers_replace = {
    db_id                    = yandex_compute_instance.db.id
    postgres_version         = tostring(var.postgres_version)
    serverless_postgres_cidr = var.serverless_postgres_cidr
    bot_public_ip            = yandex_vpc_address.bot_public_ip.external_ipv4_address[0].address
    db_private_ip            = yandex_compute_instance.db.network_interface[0].ip_address
  }

  provisioner "local-exec" {
    command = <<-EOT
      bash -lc 'set -euo pipefail
      ssh -o StrictHostKeyChecking=no \
        -o ProxyCommand="ssh -i ${local.operator_ssh_private_key_path} -W %h:%p ubuntu@${yandex_vpc_address.bot_public_ip.external_ipv4_address[0].address}" \
        -i ${local.operator_ssh_private_key_path} \
        ubuntu@${yandex_compute_instance.db.network_interface[0].ip_address} \
        "sudo bash -ceu '\''grep -Fqx \"host all all ${var.serverless_postgres_cidr} scram-sha-256\" /etc/postgresql/${var.postgres_version}/main/pg_hba.conf || echo \"host all all ${var.serverless_postgres_cidr} scram-sha-256\" >> /etc/postgresql/${var.postgres_version}/main/pg_hba.conf; systemctl reload postgresql'\''"'
    EOT
  }
}
