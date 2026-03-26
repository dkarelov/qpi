variable "support_bot_instance_group_name" {
  description = "Override for the support-bot instance group name."
  type        = string
  default     = null
}

variable "support_bot_instance_name_prefix" {
  description = "Name prefix for support-bot VM instances inside the instance group."
  type        = string
  default     = "support-bot"
}

variable "support_bot_platform_id" {
  description = "Hardware platform for support-bot VM template."
  type        = string
  default     = "standard-v3"
}

variable "support_bot_cores" {
  description = "vCPU count for the support-bot VM."
  type        = number
  default     = 2
}

variable "support_bot_memory_gb" {
  description = "RAM (GB) for the support-bot VM."
  type        = number
  default     = 2
}

variable "support_bot_disk_gb" {
  description = "Boot disk size (GB) for the support-bot VM."
  type        = number
  default     = 20
}

variable "support_bot_ssh_public_keys" {
  description = "SSH public keys injected into the support-bot VM."
  type        = list(string)
  default     = []
}

