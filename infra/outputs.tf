output "api_url" {
  value = local.api_url
}

output "n8n_url" {
  value = "https://${local.n8n_host}"
}

output "webhooks" {
  description = "Where each lead source posts. The inbound email URL with its password is in postmark_inbound_url."
  value = {
    form    = "https://${local.n8n_host}/webhook/leads/form"
    email   = "https://${local.n8n_host}/webhook/leads/email"
    booking = "https://${local.n8n_host}/webhook/leads/booking"
  }
}

output "postmark_inbound_url" {
  description = "Paste into Postmark's inbound webhook URL. Print it with: terraform output -raw postmark_inbound_url"
  value       = "https://postmark:${random_password.inbound_email_secret.result}@${local.n8n_host}/webhook/leads/email"
  sensitive   = true
}

output "cal_webhook_secret" {
  description = "Paste into the Cal.com webhook's secret field. Print it with: terraform output -raw cal_webhook_secret"
  value       = random_password.cal_webhook_secret.result
  sensitive   = true
}

output "reviewer_key" {
  description = "Signs a reviewer in on the review page. Print it with: terraform output -raw reviewer_key"
  value       = random_password.reviewer_key.result
  sensitive   = true
}

output "n8n_owner_password" {
  description = "The n8n editor password for n8n_owner_email. Print it with: terraform output -raw n8n_owner_password"
  value       = random_password.n8n_owner.result
  sensitive   = true
}

output "resource_group" {
  value = azurerm_resource_group.main.name
}

# Repository variables for the deploy workflow (none of these is a secret).
output "github_variables" {
  value = {
    AZURE_CLIENT_ID       = azurerm_user_assigned_identity.deploy.client_id
    AZURE_TENANT_ID       = data.azurerm_client_config.current.tenant_id
    AZURE_SUBSCRIPTION_ID = data.azurerm_client_config.current.subscription_id
    AZURE_RESOURCE_GROUP  = azurerm_resource_group.main.name
    AZURE_API_APP         = azurerm_container_app.api.name
    AZURE_N8N_APP         = azurerm_container_app.n8n.name
    AZURE_N8N_SETUP_JOB   = azurerm_container_app_job.n8n_setup.name
  }
}
