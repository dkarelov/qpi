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
  description = "IPv4 CIDRs allowed to SSH into VMs."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "bot_ssh_public_keys" {
  description = "SSH public keys injected into bot VM metadata as ubuntu user authorized keys."
  type        = list(string)
  default     = []
}

variable "db_ssh_public_keys" {
  description = "SSH public keys injected into DB VM metadata as ubuntu user authorized keys."
  type        = list(string)
  default     = []
}

variable "operator_ssh_private_key_path" {
  description = "Operator SSH private key path used by Terraform for DB in-place maintenance commands."
  type        = string
  default     = "~/.ssh/id_rsa"
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

variable "bot_webhook_port" {
  description = "Public webhook listen port for Telegram bot runtime."
  type        = number
  default     = 8443

  validation {
    condition     = var.bot_webhook_port >= 1 && var.bot_webhook_port <= 65535
    error_message = "bot_webhook_port must be in range 1..65535."
  }
}

variable "bot_health_port" {
  description = "Health endpoint port exposed by bot runtime."
  type        = number
  default     = 18080

  validation {
    condition     = var.bot_health_port >= 1 && var.bot_health_port <= 65535
    error_message = "bot_health_port must be in range 1..65535."
  }
}

variable "bot_app_env" {
  description = "APP_ENV passed to bot runtime env file."
  type        = string
  default     = "prod"
}

variable "bot_log_level" {
  description = "LOG_LEVEL passed to bot runtime env file."
  type        = string
  default     = "INFO"
}

variable "telegram_bot_username" {
  description = "Telegram bot username used by runtime."
  type        = string
  default     = "qpi_marketplace_bot"
}

variable "bot_webhook_secret_token" {
  description = "Webhook secret token written to bot runtime env file."
  type        = string
  default     = "change-me-webhook-secret"
  sensitive   = true
}

variable "bot_admin_telegram_ids" {
  description = "Admin Telegram IDs allowlist written to bot runtime env file."
  type        = list(number)
  default     = []
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

variable "serverless_postgres_cidr" {
  description = "CIDR used by serverless runtimes to connect to PostgreSQL."
  type        = string
  default     = "198.18.0.0/15"
}

variable "cf_runtime" {
  description = "Runtime for QPI Cloud Functions managed by Terraform."
  type        = string
  default     = "python312"
}

variable "cf_memory_mb" {
  description = "Memory (MB) allocated to managed Cloud Functions."
  type        = number
  default     = 128

  validation {
    condition     = var.cf_memory_mb >= 128
    error_message = "cf_memory_mb must be at least 128 MB."
  }
}

variable "cf_execution_timeout" {
  description = "Execution timeout for managed Cloud Functions."
  type        = number
  default     = 300

  validation {
    condition     = var.cf_execution_timeout >= 1
    error_message = "cf_execution_timeout must be >= 1 second."
  }
}

variable "cf_app_env" {
  description = "APP_ENV passed to managed Cloud Functions."
  type        = string
  default     = "prod"
}

variable "cf_log_level" {
  description = "LOG_LEVEL passed to managed Cloud Functions."
  type        = string
  default     = "INFO"
}

variable "cf_db_pool_min_size" {
  description = "DB_POOL_MIN_SIZE passed to managed Cloud Functions."
  type        = number
  default     = 1

  validation {
    condition     = var.cf_db_pool_min_size >= 1
    error_message = "cf_db_pool_min_size must be >= 1."
  }
}

variable "cf_db_pool_max_size" {
  description = "DB_POOL_MAX_SIZE passed to managed Cloud Functions."
  type        = number
  default     = 10

  validation {
    condition     = var.cf_db_pool_max_size >= 1
    error_message = "cf_db_pool_max_size must be >= 1."
  }
}

variable "cf_db_statement_timeout_ms" {
  description = "DB_STATEMENT_TIMEOUT_MS passed to managed Cloud Functions."
  type        = number
  default     = 5000

  validation {
    condition     = var.cf_db_statement_timeout_ms >= 100
    error_message = "cf_db_statement_timeout_ms must be >= 100."
  }
}

variable "cf_token_cipher_key" {
  description = "TOKEN_CIPHER_KEY used by daily-report-scrapper."
  type        = string
  sensitive   = true

  validation {
    condition     = length(trimspace(var.cf_token_cipher_key)) > 0
    error_message = "cf_token_cipher_key must not be empty."
  }
}

variable "wb_report_api_url" {
  description = "WB reportDetailByPeriod endpoint for daily-report-scrapper."
  type        = string
  default     = "https://statistics-api.wildberries.ru/api/v5/supplier/reportDetailByPeriod"
}

variable "wb_report_timeout_seconds" {
  description = "WB report request timeout in seconds."
  type        = number
  default     = 120

  validation {
    condition     = var.wb_report_timeout_seconds >= 1
    error_message = "wb_report_timeout_seconds must be >= 1."
  }
}

variable "wb_report_concurrency" {
  description = "Max concurrent WB report requests."
  type        = number
  default     = 4

  validation {
    condition     = var.wb_report_concurrency >= 1
    error_message = "wb_report_concurrency must be >= 1."
  }
}

variable "wb_report_limit" {
  description = "WB report page limit."
  type        = number
  default     = 100000

  validation {
    condition     = var.wb_report_limit >= 1
    error_message = "wb_report_limit must be >= 1."
  }
}

variable "wb_report_days_back" {
  description = "Number of days back for WB report sync window."
  type        = number
  default     = 3

  validation {
    condition     = var.wb_report_days_back >= 1
    error_message = "wb_report_days_back must be >= 1."
  }
}

variable "wb_report_max_retries" {
  description = "Max retry count for retryable WB report failures."
  type        = number
  default     = 3

  validation {
    condition     = var.wb_report_max_retries >= 0
    error_message = "wb_report_max_retries must be >= 0."
  }
}

variable "wb_report_retry_delay_seconds" {
  description = "Base retry backoff delay for WB report requests."
  type        = number
  default     = 1.0

  validation {
    condition     = var.wb_report_retry_delay_seconds > 0
    error_message = "wb_report_retry_delay_seconds must be > 0."
  }
}

variable "order_tracker_advisory_lock_id" {
  description = "Advisory lock ID used by order-tracker."
  type        = number
  default     = 7006001

  validation {
    condition     = var.order_tracker_advisory_lock_id >= 1
    error_message = "order_tracker_advisory_lock_id must be >= 1."
  }
}

variable "order_tracker_reservation_expiry_batch_size" {
  description = "Batch size for reservation expiry polling in order-tracker."
  type        = number
  default     = 100

  validation {
    condition     = var.order_tracker_reservation_expiry_batch_size >= 1
    error_message = "order_tracker_reservation_expiry_batch_size must be >= 1."
  }
}

variable "order_tracker_wb_event_batch_size" {
  description = "Batch size for WB event polling in order-tracker."
  type        = number
  default     = 200

  validation {
    condition     = var.order_tracker_wb_event_batch_size >= 1
    error_message = "order_tracker_wb_event_batch_size must be >= 1."
  }
}

variable "order_tracker_delivery_expiry_batch_size" {
  description = "Batch size for delivery-expired polling in order-tracker."
  type        = number
  default     = 200

  validation {
    condition     = var.order_tracker_delivery_expiry_batch_size >= 1
    error_message = "order_tracker_delivery_expiry_batch_size must be >= 1."
  }
}

variable "order_tracker_unlock_batch_size" {
  description = "Batch size for unlock polling in order-tracker."
  type        = number
  default     = 200

  validation {
    condition     = var.order_tracker_unlock_batch_size >= 1
    error_message = "order_tracker_unlock_batch_size must be >= 1."
  }
}

variable "order_tracker_delivery_expiry_days" {
  description = "Days until order_verified transitions to delivery_expired."
  type        = number
  default     = 60

  validation {
    condition     = var.order_tracker_delivery_expiry_days >= 1
    error_message = "order_tracker_delivery_expiry_days must be >= 1."
  }
}

variable "order_tracker_unlock_days" {
  description = "Days from pickup until reward unlock."
  type        = number
  default     = 15

  validation {
    condition     = var.order_tracker_unlock_days >= 1
    error_message = "order_tracker_unlock_days must be >= 1."
  }
}

variable "order_tracker_cron_expression" {
  description = "Cron expression for order-tracker timer trigger."
  type        = string
  default     = "*/5 * ? * * *"
}

variable "daily_report_scrapper_cron_expression" {
  description = "Cron expression for daily-report-scrapper timer trigger."
  type        = string
  default     = "0 * ? * * *"
}
