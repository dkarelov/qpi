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

variable "ubuntu_2404_lts_image_id" {
  description = "Pinned Ubuntu 24.04 LTS image ID used for long-lived VM boot disks."
  type        = string
  default     = "fd883u1fsun0dqhg49jq"
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

variable "runner_ssh_public_keys" {
  description = "SSH public keys injected into the private runner VM metadata as ubuntu user authorized keys."
  type        = list(string)
  default     = []
}

variable "runner_bootstrap_ipv4_cidrs" {
  description = "IPv4 CIDRs allowed to SSH into the private runner VM for bootstrap/maintenance."
  type        = list(string)
  default     = ["0.0.0.0/0"]
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

variable "runner_platform_id" {
  description = "Hardware platform for the private runner VM."
  type        = string
  default     = "standard-v3"
}

variable "runner_cores" {
  description = "vCPU count for the private runner VM."
  type        = number
  default     = 2
}

variable "runner_memory_gb" {
  description = "RAM (GB) for the private runner VM."
  type        = number
  default     = 4
}

variable "runner_disk_gb" {
  description = "Boot disk size (GB) for the private runner VM."
  type        = number
  default     = 30
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

variable "seller_collateral_shard_key" {
  description = "Shard key used for seller collateral top-up invoices."
  type        = string
  default     = "mvp-1"
}

variable "seller_collateral_shard_address" {
  description = "TON deposit address used by MVP shard for seller collateral top-ups."
  type        = string
  default     = "UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH"
}

variable "seller_collateral_shard_chain" {
  description = "Blockchain identifier for seller collateral shard."
  type        = string
  default     = "ton_mainnet"
}

variable "seller_collateral_shard_asset" {
  description = "Asset identifier for seller collateral shard."
  type        = string
  default     = "USDT"
}

variable "seller_collateral_invoice_ttl_hours" {
  description = "TTL hours for generated seller collateral deposit intents."
  type        = number
  default     = 24

  validation {
    condition     = var.seller_collateral_invoice_ttl_hours >= 1
    error_message = "seller_collateral_invoice_ttl_hours must be >= 1."
  }
}

variable "blockchain_checker_advisory_lock_id" {
  description = "Advisory lock ID used by blockchain-checker."
  type        = number
  default     = 7008001

  validation {
    condition     = var.blockchain_checker_advisory_lock_id >= 1
    error_message = "blockchain_checker_advisory_lock_id must be >= 1."
  }
}

variable "blockchain_checker_match_batch_size" {
  description = "Batch size of ingested chain tx rows processed per blockchain-checker run."
  type        = number
  default     = 200

  validation {
    condition     = var.blockchain_checker_match_batch_size >= 1
    error_message = "blockchain_checker_match_batch_size must be >= 1."
  }
}

variable "blockchain_checker_confirmations_required" {
  description = "Confirmation threshold before auto-crediting expected deposits."
  type        = number
  default     = 1

  validation {
    condition     = var.blockchain_checker_confirmations_required >= 1
    error_message = "blockchain_checker_confirmations_required must be >= 1."
  }
}

variable "tonapi_base_url" {
  description = "TonAPI base URL used by blockchain-checker."
  type        = string
  default     = "https://tonapi.io"
}

variable "tonapi_api_key" {
  description = "Optional TonAPI API key for blockchain-checker."
  type        = string
  default     = ""
  sensitive   = true
}

variable "tonapi_timeout_seconds" {
  description = "TonAPI request timeout in seconds."
  type        = number
  default     = 30

  validation {
    condition     = var.tonapi_timeout_seconds >= 1
    error_message = "tonapi_timeout_seconds must be >= 1."
  }
}

variable "tonapi_page_limit" {
  description = "TonAPI page size for jetton transfer history polling."
  type        = number
  default     = 100

  validation {
    condition     = var.tonapi_page_limit >= 1 && var.tonapi_page_limit <= 1000
    error_message = "tonapi_page_limit must be in range 1..1000."
  }
}

variable "tonapi_max_pages_per_shard" {
  description = "Max TonAPI pages read per shard in one checker run."
  type        = number
  default     = 20

  validation {
    condition     = var.tonapi_max_pages_per_shard >= 1
    error_message = "tonapi_max_pages_per_shard must be >= 1."
  }
}

variable "tonapi_unauth_min_interval_seconds" {
  description = "Min interval between TonAPI calls without API key."
  type        = number
  default     = 4.0

  validation {
    condition     = var.tonapi_unauth_min_interval_seconds >= 0
    error_message = "tonapi_unauth_min_interval_seconds must be >= 0."
  }
}

variable "tonapi_usdt_jetton_master" {
  description = "USDT jetton master address used by blockchain-checker."
  type        = string
  default     = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
}

variable "blockchain_checker_cron_expression" {
  description = "Cron expression for blockchain-checker timer trigger."
  type        = string
  default     = "*/5 * ? * * *"
}
