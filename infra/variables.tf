variable "folder_id" {
  description = "Yandex Cloud folder ID where resources will be created."
  type        = string

  validation {
    condition     = length(trimspace(var.folder_id)) > 0
    error_message = "folder_id must not be empty."
  }
}

variable "project" {
  description = "Name prefix for resources."
  type        = string
  default     = "qpi"
}

variable "zone" {
  description = "Primary availability zone."
  type        = string
  default     = "ru-central1-d"
}

variable "network_name" {
  description = "Existing VPC network name."
  type        = string
  default     = "default"
}

variable "subnet_name" {
  description = "Existing subnet name in selected zone."
  type        = string
  default     = "default-ru-central1-d"
}

variable "private_subnet_cidr" {
  description = "CIDR for private subnet (DB and future private services)."
  type        = string
  default     = "10.131.0.0/24"
}

variable "admin_ipv4_cidrs" {
  description = "IPv4 CIDRs allowed to SSH (OS Login) into VMs."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "deletion_protection" {
  description = "Prevent accidental deletion of critical resources."
  type        = bool
  default     = false
}

variable "bot_platform_id" {
  description = "Hardware platform for bot VM template."
  type        = string
  default     = "standard-v3"
}

variable "bot_cores" {
  description = "vCPU count for preemptible bot VM."
  type        = number
  default     = 2
}

variable "bot_memory_gb" {
  description = "RAM (GB) for preemptible bot VM."
  type        = number
  default     = 2
}

variable "bot_disk_gb" {
  description = "Boot disk size (GB) for preemptible bot VM."
  type        = number
  default     = 20
}

variable "db_platform_id" {
  description = "Hardware platform for DB VM."
  type        = string
  default     = "standard-v3"
}

variable "db_cores" {
  description = "vCPU count for DB VM."
  type        = number
  default     = 2
}

variable "db_memory_gb" {
  description = "RAM (GB) for DB VM."
  type        = number
  default     = 4
}

variable "db_disk_gb" {
  description = "Boot disk size (GB) for DB VM."
  type        = number
  default     = 40
}

variable "postgres_version" {
  description = "PostgreSQL major version installed from pgdg."
  type        = number
  default     = 18

  validation {
    condition     = var.postgres_version >= 18
    error_message = "postgres_version must be 18 or newer."
  }
}

variable "db_name" {
  description = "Application database name."
  type        = string
  default     = "qpi"

  validation {
    condition     = can(regex("^[A-Za-z_][A-Za-z0-9_]{0,62}$", var.db_name))
    error_message = "db_name must match PostgreSQL identifier rules (letters/digits/underscore, max 63 chars)."
  }
}

variable "db_user" {
  description = "Application database user."
  type        = string
  default     = "qpi"

  validation {
    condition     = can(regex("^[A-Za-z_][A-Za-z0-9_]{0,62}$", var.db_user))
    error_message = "db_user must match PostgreSQL identifier rules (letters/digits/underscore, max 63 chars)."
  }
}
