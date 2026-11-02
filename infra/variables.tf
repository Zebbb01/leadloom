variable "location" {
  description = "Azure region. Southeast Asia is closest to the Philippines; pick one near the database's region if it differs."
  type        = string
  default     = "southeastasia"
}

variable "name_prefix" {
  description = "Prefix for resource names; a random suffix keeps global names unique."
  type        = string
  default     = "leadloom"
}

variable "api_image" {
  description = "Container image for the API. The deploy workflow replaces it on each release."
  type        = string
  default     = "ghcr.io/zebbb01/leadloom-api:latest"
}

variable "n8n_image" {
  description = "Container image for n8n and its setup job. The deploy workflow replaces it on each release."
  type        = string
  default     = "ghcr.io/zebbb01/leadloom-n8n:latest"
}

variable "github_oidc_subject_prefix" {
  description = "Start of the subject GitHub puts in the repository's OpenID Connect tokens. New repositories use immutable IDs (repo:OWNER@OWNER_ID/REPO@REPO_ID); print yours with: gh api repos/OWNER/REPO/actions/oidc/customization/sub"
  type        = string
}

variable "alert_email" {
  description = "Email for the cost alert (a 1-unit monthly budget that warns at the first cent spent). Empty skips the alert."
  type        = string
  default     = ""
}

# The database: Supabase's free PostgreSQL (session pooler), or ReplyPilot's free
# PostgreSQL Flexible Server when that is deployed. LeadLoom uses the schema
# "leadloom" and n8n the schema "n8n"; the API creates both.

variable "postgres_host" {
  description = "Supabase: the session pooler host, such as aws-0-ap-southeast-1.pooler.supabase.com. ReplyPilot's server: psql-replypilot-xxxxxx.postgres.database.azure.com."
  type        = string
}

variable "postgres_port" {
  description = "5432 for Supabase's session pooler and for Azure."
  type        = number
  default     = 5432
}

variable "postgres_database" {
  description = "Supabase: postgres. ReplyPilot's server: a database created for LeadLoom, such as leadloom."
  type        = string
  default     = "postgres"
}

variable "postgres_user" {
  description = "Supabase's pooler: postgres.<project-ref>. ReplyPilot's server: its administrator login."
  type        = string
}

variable "postgres_password" {
  description = "The database password."
  type        = string
  sensitive   = true
}

variable "postgres_tls_verify" {
  description = "Verify the database's certificate against the system CAs: true for Azure's servers, false for Supabase's pooler (its CA is Supabase's own; the connection is still encrypted)."
  type        = bool
  default     = false
}

variable "llm_api_key" {
  description = "Key for the model. Gemini API free tier: a key from https://aistudio.google.com/apikey. Empty scores leads by rules only and sends them all to review."
  type        = string
  default     = ""
  sensitive   = true
}

variable "llm_endpoint" {
  description = "OpenAI-compatible endpoint with JSON-schema output."
  type        = string
  default     = "https://generativelanguage.googleapis.com/v1beta/openai"
}

variable "llm_model" {
  description = "Model name at that endpoint."
  type        = string
  default     = "gemini-3.5-flash-lite"
}

variable "llm_reasoning_effort" {
  description = "Sent as reasoning_effort; empty leaves it out (for models that reject it)."
  type        = string
  default     = "low"
}

variable "daily_llm_cap" {
  description = "Model calls per day across the app, kept under the free tier's daily limit. Past it, leads are scored by rules and go to review."
  type        = number
  default     = 200
}

variable "hubspot_token" {
  description = "A HubSpot service key (developer test account) with read and write scopes for contacts, companies and deals. Empty skips the CRM sync."
  type        = string
  default     = ""
  sensitive   = true
}

variable "hubspot_portal_id" {
  description = "The HubSpot account ID, for record links on the review page and in Slack."
  type        = string
  default     = ""
}

variable "slack_webhook_url" {
  description = "A Slack incoming webhook for hot-lead alerts. Empty skips the alert."
  type        = string
  default     = ""
  sensitive   = true
}

variable "n8n_owner_email" {
  description = "Sign-in email for the n8n editor. The password is generated: terraform output -raw n8n_owner_password."
  type        = string
}
