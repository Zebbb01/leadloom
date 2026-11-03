# n8n with LeadLoom's workflows baked in. The app runs the image's normal entrypoint
# (n8n start); the deploy runs setup.sh once in a Container Apps job to import and
# publish the workflows, so a cold start does not pay for it.
FROM n8nio/n8n:2.45.7
COPY --chown=node:node workflows /home/node/workflows
COPY --chown=node:node setup.sh /home/node/setup.sh
ENV N8N_BLOCK_ENV_ACCESS_IN_NODE=false \
    N8N_DIAGNOSTICS_ENABLED=false \
    N8N_VERSION_NOTIFICATIONS_ENABLED=false \
    N8N_TEMPLATES_ENABLED=false \
    N8N_PERSONALIZATION_ENABLED=false \
    GENERIC_TIMEZONE=UTC \
    EXECUTIONS_DATA_PRUNE=true \
    EXECUTIONS_DATA_MAX_AGE=336
