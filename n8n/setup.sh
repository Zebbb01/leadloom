#!/bin/sh
# Loads the workflows in this image into n8n's database and publishes the intake
# workflow. Run once per deploy (the Container Apps job) and before `n8n start`
# locally. Unpublishing first lets the import replace a published workflow; a
# running n8n picks the new version up within 15 seconds.
set -e
n8n unpublish:workflow --id=LeadLoomIntake01
n8n import:workflow --separate --input=/home/node/workflows
n8n publish:workflow --id=LeadLoomIntake01
