output "support_bot_instance_group_id" {
  description = "ID of the support-bot instance group."
  value       = yandex_compute_instance_group.support_bot.id
}

output "support_bot_instance_group_name" {
  description = "Name of the support-bot instance group."
  value       = yandex_compute_instance_group.support_bot.name
}

output "support_bot_container_registry_id" {
  description = "ID of the support-bot container registry."
  value       = yandex_container_registry.support_bot.id
}

output "support_bot_container_registry_name" {
  description = "Name of the support-bot container registry."
  value       = yandex_container_registry.support_bot.name
}

output "support_bot_container_repository_name" {
  description = "Repository path inside the support-bot container registry."
  value       = yandex_container_repository.support_bot.name
}

output "support_bot_container_image_prefix" {
  description = "Immutable image prefix for support-bot deploys."
  value       = "cr.yandex/${yandex_container_registry.support_bot.id}/support-bot"
}
