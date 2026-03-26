locals {
  support_bot_instance_group_name = coalesce(var.support_bot_instance_group_name, "${var.project}-support-bot-ig")

  support_bot_cloud_init = templatefile("${path.module}/cloud-init/support_bot.yaml.tftpl", {})

  support_bot_ssh_keys_metadata = join("\n", [
    for key in var.support_bot_ssh_public_keys : "ubuntu:${trimspace(key)}"
  ])
}

resource "yandex_vpc_security_group" "support_bot" {
  folder_id  = var.folder_id
  name       = "${var.project}-sg-support-bot"
  network_id = data.yandex_vpc_network.main.id

  ingress {
    protocol          = "TCP"
    description       = "SSH from private runner SG"
    port              = 22
    security_group_id = yandex_vpc_security_group.runner.id
  }

  ingress {
    protocol          = "TCP"
    description       = "SSH from qpi bot SG for bastion-style access"
    port              = 22
    security_group_id = yandex_vpc_security_group.bot.id
  }

  egress {
    protocol       = "ANY"
    description    = "Allow all egress"
    v4_cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "yandex_compute_instance_group" "support_bot" {
  folder_id           = var.folder_id
  name                = local.support_bot_instance_group_name
  service_account_id  = yandex_iam_service_account.bot_vm.id
  deletion_protection = var.deletion_protection
  depends_on = [
    yandex_resourcemanager_folder_iam_member.bot_vm_editor
  ]

  instance_template {
    name        = "${var.project}-${var.support_bot_instance_name_prefix}-{instance.index}"
    platform_id = var.support_bot_platform_id

    resources {
      cores         = var.support_bot_cores
      memory        = var.support_bot_memory_gb
      core_fraction = 100
    }

    boot_disk {
      mode = "READ_WRITE"
      initialize_params {
        image_id = var.ubuntu_2404_lts_image_id
        type     = "network-ssd"
        size     = var.support_bot_disk_gb
      }
    }

    network_interface {
      network_id         = data.yandex_vpc_network.main.id
      subnet_ids         = [yandex_vpc_subnet.private.id]
      nat                = false
      security_group_ids = [yandex_vpc_security_group.support_bot.id]
    }

    scheduling_policy {
      preemptible = true
    }

    service_account_id = yandex_iam_service_account.bot_vm.id

    metadata = merge(
      {
        enable-oslogin     = "false"
        serial-port-enable = "1"
        user-data          = local.support_bot_cloud_init
      },
      length(var.support_bot_ssh_public_keys) > 0 ? {
        "ssh-keys" = local.support_bot_ssh_keys_metadata
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

