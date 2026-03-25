terraform {
  required_version = ">= 1.6.0"

  required_providers {
    external = {
      source  = "hashicorp/external"
      version = ">= 2.3.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.6.0"
    }
    yandex = {
      source = "yandex-cloud/yandex"
    }
  }
}
