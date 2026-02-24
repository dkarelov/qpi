data "yandex_compute_image" "ubuntu_2404_lts" {
  family = "ubuntu-2404-lts"
}

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
    port           = 443
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
    folder_id    = var.folder_id
    log_group_id = yandex_logging_group.main.id
  })

  bot_ssh_keys_metadata = join("\n", [
    for key in var.bot_ssh_public_keys : "ubuntu:${trimspace(key)}"
  ])

  db_ssh_keys_metadata = join("\n", [
    for key in var.db_ssh_public_keys : "ubuntu:${trimspace(key)}"
  ])

  db_cloud_init = templatefile("${path.module}/cloud-init/db.yaml.tftpl", {
    postgres_version = var.postgres_version
    db_name          = var.db_name
    db_user          = var.db_user
    db_password      = random_password.db_password.result
  })
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
        image_id = data.yandex_compute_image.ubuntu_2404_lts.id
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
      image_id = data.yandex_compute_image.ubuntu_2404_lts.id
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
