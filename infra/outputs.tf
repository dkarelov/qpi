output "bot_instance_group_id" {
  description = "ID of the preemptible bot instance group."
  value       = yandex_compute_instance_group.bot.id
}

output "bot_instance_group_name" {
  description = "Name of the preemptible bot instance group."
  value       = yandex_compute_instance_group.bot.name
}

output "bot_public_ip" {
  description = "Static public IP used for Telegram webhook."
  value       = yandex_vpc_address.bot_public_ip.external_ipv4_address[0].address
}

output "db_private_ip" {
  description = "Private IP of the DB VM."
  value       = yandex_compute_instance.db.network_interface[0].ip_address
}

output "db_name" {
  description = "Application database name."
  value       = var.db_name
}

output "db_user" {
  description = "Application database user."
  value       = var.db_user
}

output "db_password" {
  description = "Generated PostgreSQL password."
  value       = random_password.db_password.result
  sensitive   = true
}

output "logging_group_id" {
  description = "Cloud Logging group ID."
  value       = yandex_logging_group.main.id
}

output "nat_gateway_id" {
  description = "NAT gateway ID used by the private subnet route table."
  value       = yandex_vpc_gateway.nat.id
}

output "private_subnet_id" {
  description = "Private subnet ID for DB and future private services."
  value       = yandex_vpc_subnet.private.id
}
