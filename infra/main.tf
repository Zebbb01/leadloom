# LeadLoom on Azure, inside free tiers and free monthly grants: Container Apps
# (consumption) for the API and n8n, a Container Apps job that loads the n8n
# workflows at each deploy, and Log Analytics with a daily cap. The database is
# not created here: it is Supabase's free PostgreSQL, or the free PostgreSQL
# Flexible Server that ReplyPilot creates (Azure allows one free server per
# subscription, so this configuration never makes a second one).
#
# A Free Trial subscription also allows only one Container Apps environment, so
# with `shared_environment` set the apps join an existing one (LedgerLens's)
# instead of creating their own.

terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "azurerm" {
  features {}
}

data "azurerm_client_config" "current" {}

resource "random_string" "suffix" {
  length  = 6
  special = false
  upper   = false
}

# n8n calls the API with this key; the review page's Retry button calls n8n with it.
resource "random_password" "api_key" {
  length  = 40
  special = false
}

resource "random_password" "reviewer_key" {
  length  = 24
  special = false
}

resource "random_password" "n8n_encryption_key" {
  length  = 40
  special = false
}

# The n8n owner signs in with this password; n8n gets only its bcrypt hash.
resource "random_password" "n8n_owner" {
  length  = 24
  special = false
}

# Paste into Cal.com's webhook "secret" field.
resource "random_password" "cal_webhook_secret" {
  length  = 40
  special = false
}

# Goes into the Postmark inbound webhook URL as the basic-auth password.
resource "random_password" "inbound_email_secret" {
  length  = 32
  special = false
}

locals {
  name     = "${var.name_prefix}-${random_string.suffix.result}"
  tags     = { app = "leadloom", cost = "free-tier" }
  api_name = "ca-${local.name}-api"
  n8n_name = "ca-${local.name}-n8n"
  api_url  = "https://${local.api_name}.${local.environment_domain}"
  n8n_host = "${local.n8n_name}.${local.environment_domain}"

  # Supabase's pooler presents a certificate from Supabase's own CA, so the
  # connection is encrypted without verifying it; Azure's servers verify against
  # the system CAs.
  sslmode      = var.postgres_tls_verify ? "verify-full&sslrootcert=system" : "require"
  database_url = "postgresql://${urlencode(var.postgres_user)}:${urlencode(var.postgres_password)}@${var.postgres_host}:${var.postgres_port}/${var.postgres_database}?sslmode=${local.sslmode}"

  api_env = merge(
    {
      PUBLIC_URL               = local.api_url
      LEADLOOM_N8N_WEBHOOK_URL = "https://${local.n8n_host}/webhook"
      LLM_ENDPOINT             = var.llm_endpoint
      LLM_MODEL                = var.llm_model
      LLM_REASONING_EFFORT     = var.llm_reasoning_effort
      DAILY_LLM_CAP            = tostring(var.daily_llm_cap)
    },
    var.hubspot_portal_id == "" ? {} : { HUBSPOT_PORTAL_ID = var.hubspot_portal_id },
  )
  api_optional_secrets = {
    "llm-api-key"   = { env = "LLM_API_KEY", value = var.llm_api_key }
    "hubspot-token" = { env = "HUBSPOT_TOKEN", value = var.hubspot_token }
  }
  api_secrets = merge(
    {
      "database-url"         = { env = "DATABASE_URL", value = local.database_url }
      "leadloom-api-key"     = { env = "LEADLOOM_API_KEY", value = random_password.api_key.result }
      "reviewer-key"         = { env = "REVIEWER_KEY", value = random_password.reviewer_key.result }
      "cal-webhook-secret"   = { env = "CAL_WEBHOOK_SECRET", value = random_password.cal_webhook_secret.result }
      "inbound-email-secret" = { env = "INBOUND_EMAIL_SECRET", value = random_password.inbound_email_secret.result }
    },
    # Only whether a secret is set leaves the sensitive values.
    { for k, v in local.api_optional_secrets : k => v if nonsensitive(v.value != "") },
  )

  n8n_env = {
    DB_TYPE                               = "postgresdb"
    DB_POSTGRESDB_HOST                    = var.postgres_host
    DB_POSTGRESDB_PORT                    = tostring(var.postgres_port)
    DB_POSTGRESDB_DATABASE                = var.postgres_database
    DB_POSTGRESDB_USER                    = var.postgres_user
    DB_POSTGRESDB_SCHEMA                  = "n8n"
    DB_POSTGRESDB_POOL_SIZE               = "2"
    DB_POSTGRESDB_SSL_ENABLED             = "true"
    DB_POSTGRESDB_SSL_REJECT_UNAUTHORIZED = tostring(var.postgres_tls_verify)
    N8N_HOST                              = local.n8n_host
    N8N_PROTOCOL                          = "https"
    N8N_PROXY_HOPS                        = "1"
    WEBHOOK_URL                           = "https://${local.n8n_host}/"
    N8N_EDITOR_BASE_URL                   = "https://${local.n8n_host}/"
    LEADLOOM_API_URL                      = local.api_url
    N8N_INSTANCE_OWNER_MANAGED_BY_ENV     = "true"
    N8N_INSTANCE_OWNER_EMAIL              = var.n8n_owner_email
    N8N_INSTANCE_OWNER_FIRST_NAME         = "LeadLoom"
    N8N_INSTANCE_OWNER_LAST_NAME          = "Owner"
  }
  n8n_secrets = merge(
    {
      "db-password"         = { env = "DB_POSTGRESDB_PASSWORD", value = var.postgres_password }
      "n8n-encryption-key"  = { env = "N8N_ENCRYPTION_KEY", value = random_password.n8n_encryption_key.result }
      "leadloom-api-key"    = { env = "LEADLOOM_API_KEY", value = random_password.api_key.result }
      "n8n-owner-pass-hash" = { env = "N8N_INSTANCE_OWNER_PASSWORD_HASH", value = random_password.n8n_owner.bcrypt_hash }
    },
    nonsensitive(var.slack_webhook_url != "") ? {
      "slack-webhook-url" = { env = "SLACK_WEBHOOK_URL", value = var.slack_webhook_url }
    } : {},
  )
}

resource "azurerm_resource_group" "main" {
  name     = "rg-${local.name}"
  location = var.location
  tags     = local.tags
}

data "azurerm_container_app_environment" "shared" {
  count               = var.shared_environment == null ? 0 : 1
  name                = var.shared_environment.name
  resource_group_name = var.shared_environment.resource_group
}

resource "azurerm_log_analytics_workspace" "main" {
  count               = var.shared_environment == null ? 1 : 0
  name                = "log-${local.name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  daily_quota_gb      = 0.05 # about 1.5 GB a month: the 5 GB free grant is shared with the other apps
  tags                = local.tags
}

resource "azurerm_container_app_environment" "main" {
  count                      = var.shared_environment == null ? 1 : 0
  name                       = "cae-${local.name}"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main[0].id
  logs_destination           = "log-analytics"
  tags                       = local.tags

  # Azure creates every environment with this serverless profile. It is billed
  # per second like the consumption plan and covered by the free grant.
  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }
}

locals {
  environment_id     = var.shared_environment == null ? azurerm_container_app_environment.main[0].id : data.azurerm_container_app_environment.shared[0].id
  environment_domain = var.shared_environment == null ? azurerm_container_app_environment.main[0].default_domain : data.azurerm_container_app_environment.shared[0].default_domain
}

# The API and review pages: one small replica at most, none when idle.
resource "azurerm_container_app" "api" {
  name                         = local.api_name
  container_app_environment_id = local.environment_id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"
  tags                         = local.tags

  dynamic "secret" {
    for_each = local.api_secrets
    content {
      name  = secret.key
      value = secret.value.value
    }
  }

  ingress {
    external_enabled = true
    target_port      = 8000
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = 0
    max_replicas = 1

    container {
      name   = "api"
      image  = var.api_image
      cpu    = 0.25
      memory = "0.5Gi"

      dynamic "env" {
        for_each = local.api_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.api_secrets
        content {
          name        = env.value.env
          secret_name = env.key
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].container[0].image] # set by the deploy workflow
  }
}

# n8n receives the webhooks. It scales to zero too; the first webhook after a quiet
# spell wakes it, and its waiting review runs live in the database.
resource "azurerm_container_app" "n8n" {
  name                         = local.n8n_name
  container_app_environment_id = local.environment_id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"
  tags                         = local.tags

  dynamic "secret" {
    for_each = local.n8n_secrets
    content {
      name  = secret.key
      value = secret.value.value
    }
  }

  ingress {
    external_enabled = true
    target_port      = 5678
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = 0
    max_replicas = 1

    container {
      name   = "n8n"
      image  = var.n8n_image
      cpu    = 0.5
      memory = "1Gi"

      dynamic "env" {
        for_each = local.n8n_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.n8n_secrets
        content {
          name        = env.value.env
          secret_name = env.key
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].container[0].image]
  }
}

# Imports and publishes the workflows baked into the n8n image. The deploy workflow
# starts it after each new n8n image, so n8n's own cold start stays short.
resource "azurerm_container_app_job" "n8n_setup" {
  name                         = "caj-${local.name}-n8n-setup"
  location                     = azurerm_resource_group.main.location
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = local.environment_id
  workload_profile_name        = "Consumption"
  # A fresh n8n database takes several minutes of migrations when the database
  # is far away (Supabase in another region); later runs take about a minute.
  replica_timeout_in_seconds = 1800
  replica_retry_limit        = 1
  tags                       = local.tags

  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
  }

  dynamic "secret" {
    for_each = local.n8n_secrets
    content {
      name  = secret.key
      value = secret.value.value
    }
  }

  template {
    container {
      name    = "setup"
      image   = var.n8n_image
      cpu     = 0.5
      memory  = "1Gi"
      command = ["sh", "/home/node/setup.sh"]

      dynamic "env" {
        for_each = local.n8n_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.n8n_secrets
        content {
          name        = env.value.env
          secret_name = env.key
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].container[0].image]
  }
}

# GitHub Actions signs in with OpenID Connect to roll out new images: no secret
# is stored in the repository.
resource "azurerm_user_assigned_identity" "deploy" {
  name                = "id-${local.name}-github"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_federated_identity_credential" "github_main" {
  name                      = "github-main"
  user_assigned_identity_id = azurerm_user_assigned_identity.deploy.id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = "https://token.actions.githubusercontent.com"
  subject                   = "${var.github_oidc_subject_prefix}:ref:refs/heads/main"
}

resource "azurerm_role_assignment" "deploy" {
  scope                = azurerm_resource_group.main.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.deploy.principal_id
}

# Updating an app re-checks that it may join its environment, which lives in
# another resource group when it is shared.
resource "azurerm_role_assignment" "deploy_environment" {
  count                = var.shared_environment == null ? 0 : 1
  scope                = local.environment_id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.deploy.principal_id
}

# A 1-unit monthly budget on the resource group: an email goes out at the first
# cent of actual cost, so a change that leaves the free tiers is noticed at once.
resource "azurerm_consumption_budget_resource_group" "guard" {
  count             = var.alert_email == "" ? 0 : 1
  name              = "budget-${local.name}"
  resource_group_id = azurerm_resource_group.main.id
  amount            = 1
  time_grain        = "Monthly"

  time_period {
    start_date = formatdate("YYYY-MM-01'T'00:00:00Z", plantimestamp())
  }

  notification {
    enabled        = true
    threshold      = 1
    operator       = "GreaterThan"
    threshold_type = "Actual"
    contact_emails = [var.alert_email]
  }

  notification {
    enabled        = true
    threshold      = 100
    operator       = "GreaterThan"
    threshold_type = "Forecasted"
    contact_emails = [var.alert_email]
  }

  lifecycle {
    ignore_changes = [time_period]
  }
}
