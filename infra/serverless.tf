resource "yandex_iam_service_account" "cf_trigger_invoker" {
  folder_id   = var.folder_id
  name        = "${var.project}-cf-trigger-sa"
  description = "Service account used by serverless triggers to invoke QPI functions."
}

resource "yandex_resourcemanager_folder_iam_member" "cf_trigger_invoker_functions_invoker" {
  folder_id = var.folder_id
  role      = "serverless.functions.invoker"
  member    = "serviceAccount:${yandex_iam_service_account.cf_trigger_invoker.id}"
}

locals {
  cf_database_url = format(
    "postgresql://%s:%s@%s:5432/%s",
    var.db_user,
    random_password.db_password.result,
    yandex_compute_instance.db.network_interface[0].ip_address,
    var.db_name,
  )

  cf_common_package_excludes = [
    ".git",
    ".github",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "infra",
    "qpi.egg-info",
    "tests",
    "__pycache__",
    "**/__pycache__",
    "**/__pycache__/*",
    "*.pyc",
    "**/*.pyc",
    "AGENTS.md",
    "PLAN.md",
    "README.md",
    ".env.example",
    ".gitignore",
    "Makefile",
    "services/bot_api",
    "services/worker",
  ]

  daily_report_scrapper_package_excludes = concat(local.cf_common_package_excludes, [
    "services/order_tracker",
    "services/blockchain_checker",
  ])

  order_tracker_package_excludes = concat(local.cf_common_package_excludes, [
    "services/daily_report_scrapper",
    "services/blockchain_checker",
  ])

  blockchain_checker_package_excludes = concat(local.cf_common_package_excludes, [
    "services/daily_report_scrapper",
    "services/order_tracker",
  ])
}

data "archive_file" "daily_report_scrapper_source" {
  type        = "zip"
  source_dir  = "${path.module}/.."
  output_path = "${path.module}/.terraform/daily-report-scrapper-source.zip"
  excludes    = local.daily_report_scrapper_package_excludes
}

data "archive_file" "order_tracker_source" {
  type        = "zip"
  source_dir  = "${path.module}/.."
  output_path = "${path.module}/.terraform/order-tracker-source.zip"
  excludes    = local.order_tracker_package_excludes
}

data "archive_file" "blockchain_checker_source" {
  type        = "zip"
  source_dir  = "${path.module}/.."
  output_path = "${path.module}/.terraform/blockchain-checker-source.zip"
  excludes    = local.blockchain_checker_package_excludes
}

resource "yandex_function" "daily_report_scrapper" {
  folder_id          = var.folder_id
  name               = "${var.project}-daily-report-scrapper"
  description        = "QPI Phase 5 daily report scrapper"
  runtime            = var.cf_runtime
  entrypoint         = "services.daily_report_scrapper.main.handler"
  memory             = var.cf_memory_mb
  execution_timeout  = var.cf_execution_timeout
  service_account_id = yandex_iam_service_account.bot_vm.id
  user_hash          = data.archive_file.daily_report_scrapper_source.output_base64sha256

  content {
    zip_filename = data.archive_file.daily_report_scrapper_source.output_path
  }

  connectivity {
    network_id = data.yandex_vpc_network.main.id
  }

  environment = {
    APP_ENV                       = var.cf_app_env
    LOG_LEVEL                     = var.cf_log_level
    DATABASE_URL                  = local.cf_database_url
    DB_POOL_MIN_SIZE              = tostring(var.cf_db_pool_min_size)
    DB_POOL_MAX_SIZE              = tostring(var.cf_db_pool_max_size)
    DB_STATEMENT_TIMEOUT_MS       = tostring(var.cf_db_statement_timeout_ms)
    TOKEN_CIPHER_KEY              = var.cf_token_cipher_key
    WB_REPORT_API_URL             = var.wb_report_api_url
    WB_REPORT_TIMEOUT_SECONDS     = tostring(var.wb_report_timeout_seconds)
    WB_REPORT_CONCURRENCY         = tostring(var.wb_report_concurrency)
    WB_REPORT_LIMIT               = tostring(var.wb_report_limit)
    WB_REPORT_DAYS_BACK           = tostring(var.wb_report_days_back)
    WB_REPORT_MAX_RETRIES         = tostring(var.wb_report_max_retries)
    WB_REPORT_RETRY_DELAY_SECONDS = tostring(var.wb_report_retry_delay_seconds)
  }

  log_options {
    disabled     = false
    log_group_id = yandex_logging_group.main.id
  }

  tags = ["terraform", "runtime"]
}

resource "yandex_function_trigger" "daily_report_scrapper_timer" {
  folder_id   = var.folder_id
  name        = "${var.project}-daily-report-scrapper-every-1h"
  description = "Runs daily report scrapper every hour"
  depends_on  = [yandex_resourcemanager_folder_iam_member.cf_trigger_invoker_functions_invoker]

  timer {
    cron_expression = var.daily_report_scrapper_cron_expression
  }

  function {
    id                 = yandex_function.daily_report_scrapper.id
    tag                = "$latest"
    service_account_id = yandex_iam_service_account.cf_trigger_invoker.id
  }
}

resource "yandex_function" "order_tracker" {
  folder_id          = var.folder_id
  name               = "${var.project}-order-tracker"
  description        = "QPI Phase 6 order tracker"
  runtime            = var.cf_runtime
  entrypoint         = "services.order_tracker.main.handler"
  memory             = var.cf_memory_mb
  execution_timeout  = var.cf_execution_timeout
  service_account_id = yandex_iam_service_account.bot_vm.id
  user_hash          = data.archive_file.order_tracker_source.output_base64sha256

  content {
    zip_filename = data.archive_file.order_tracker_source.output_path
  }

  connectivity {
    network_id = data.yandex_vpc_network.main.id
  }

  environment = {
    APP_ENV                                     = var.cf_app_env
    LOG_LEVEL                                   = var.cf_log_level
    DATABASE_URL                                = local.cf_database_url
    DB_POOL_MIN_SIZE                            = tostring(var.cf_db_pool_min_size)
    DB_POOL_MAX_SIZE                            = tostring(var.cf_db_pool_max_size)
    DB_STATEMENT_TIMEOUT_MS                     = tostring(var.cf_db_statement_timeout_ms)
    ORDER_TRACKER_ADVISORY_LOCK_ID              = tostring(var.order_tracker_advisory_lock_id)
    ORDER_TRACKER_RESERVATION_EXPIRY_BATCH_SIZE = tostring(var.order_tracker_reservation_expiry_batch_size)
    ORDER_TRACKER_WB_EVENT_BATCH_SIZE           = tostring(var.order_tracker_wb_event_batch_size)
    ORDER_TRACKER_DELIVERY_EXPIRY_BATCH_SIZE    = tostring(var.order_tracker_delivery_expiry_batch_size)
    ORDER_TRACKER_UNLOCK_BATCH_SIZE             = tostring(var.order_tracker_unlock_batch_size)
    ORDER_TRACKER_DELIVERY_EXPIRY_DAYS          = tostring(var.order_tracker_delivery_expiry_days)
    ORDER_TRACKER_UNLOCK_DAYS                   = tostring(var.order_tracker_unlock_days)
  }

  log_options {
    disabled     = false
    log_group_id = yandex_logging_group.main.id
  }

  tags = ["terraform", "runtime"]
}

resource "yandex_function_trigger" "order_tracker_timer" {
  folder_id   = var.folder_id
  name        = "${var.project}-order-tracker-every-5m"
  description = "Runs order tracker every 5 minutes"
  depends_on  = [yandex_resourcemanager_folder_iam_member.cf_trigger_invoker_functions_invoker]

  timer {
    cron_expression = var.order_tracker_cron_expression
  }

  function {
    id                 = yandex_function.order_tracker.id
    tag                = "$latest"
    service_account_id = yandex_iam_service_account.cf_trigger_invoker.id
  }
}

resource "yandex_function" "blockchain_checker" {
  folder_id          = var.folder_id
  name               = "${var.project}-blockchain-checker"
  description        = "QPI Phase 8 blockchain checker"
  runtime            = var.cf_runtime
  entrypoint         = "services.blockchain_checker.main.handler"
  memory             = var.cf_memory_mb
  execution_timeout  = var.cf_execution_timeout
  service_account_id = yandex_iam_service_account.bot_vm.id
  user_hash          = data.archive_file.blockchain_checker_source.output_base64sha256

  content {
    zip_filename = data.archive_file.blockchain_checker_source.output_path
  }

  connectivity {
    network_id = data.yandex_vpc_network.main.id
  }

  environment = merge(
    {
      APP_ENV                             = var.cf_app_env
      LOG_LEVEL                           = var.cf_log_level
      DATABASE_URL                        = local.cf_database_url
      DB_POOL_MIN_SIZE                    = tostring(var.cf_db_pool_min_size)
      DB_POOL_MAX_SIZE                    = tostring(var.cf_db_pool_max_size)
      DB_STATEMENT_TIMEOUT_MS             = tostring(var.cf_db_statement_timeout_ms)
      SELLER_COLLATERAL_SHARD_KEY         = var.seller_collateral_shard_key
      SELLER_COLLATERAL_SHARD_ADDRESS     = var.seller_collateral_shard_address
      SELLER_COLLATERAL_SHARD_CHAIN       = var.seller_collateral_shard_chain
      SELLER_COLLATERAL_SHARD_ASSET       = var.seller_collateral_shard_asset
      SELLER_COLLATERAL_INVOICE_TTL_HOURS = tostring(var.seller_collateral_invoice_ttl_hours)
      BLOCKCHAIN_CHECKER_ADVISORY_LOCK_ID = tostring(var.blockchain_checker_advisory_lock_id)
      BLOCKCHAIN_CHECKER_MATCH_BATCH_SIZE = tostring(var.blockchain_checker_match_batch_size)
      BLOCKCHAIN_CHECKER_CONFIRMATIONS_REQUIRED = tostring(
        var.blockchain_checker_confirmations_required
      )
      TONAPI_BASE_URL                    = var.tonapi_base_url
      TONAPI_TIMEOUT_SECONDS             = tostring(var.tonapi_timeout_seconds)
      TONAPI_PAGE_LIMIT                  = tostring(var.tonapi_page_limit)
      TONAPI_MAX_PAGES_PER_SHARD         = tostring(var.tonapi_max_pages_per_shard)
      TONAPI_UNAUTH_MIN_INTERVAL_SECONDS = tostring(var.tonapi_unauth_min_interval_seconds)
      TONAPI_USDT_JETTON_MASTER          = var.tonapi_usdt_jetton_master
    },
    trimspace(var.tonapi_api_key) == "" ? {} : {
      TONAPI_API_KEY = var.tonapi_api_key
    },
  )

  log_options {
    disabled     = false
    log_group_id = yandex_logging_group.main.id
  }

  tags = ["terraform", "runtime"]
}

resource "yandex_function_trigger" "blockchain_checker_timer" {
  folder_id   = var.folder_id
  name        = "${var.project}-blockchain-checker-every-5m"
  description = "Runs blockchain checker every 5 minutes"
  depends_on  = [yandex_resourcemanager_folder_iam_member.cf_trigger_invoker_functions_invoker]

  timer {
    cron_expression = var.blockchain_checker_cron_expression
  }

  function {
    id                 = yandex_function.blockchain_checker.id
    tag                = "$latest"
    service_account_id = yandex_iam_service_account.cf_trigger_invoker.id
  }
}
