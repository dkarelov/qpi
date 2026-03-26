output "support_bot_instance_group_id" {
  description = "ID of the support-bot instance group."
  value       = yandex_compute_instance_group.support_bot.id
}

output "support_bot_instance_group_name" {
  description = "Name of the support-bot instance group."
  value       = yandex_compute_instance_group.support_bot.name
}

