#!/usr/bin/env bash
# Used by .github/workflows/deploy-aws.yml: sam deploy with fallback to
# aws cloudformation update-stack when changeset early validation fails
# (e.g. AWS::EarlyValidation::ResourceExistenceCheck).
#
# Required env: AWS_REGION, AWS_S3_BUCKET, AWS_STACK_NAME, STAGE_NAME,
# DATABASE_SCHEMA, and secret/credential vars below (set by the workflow).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${SAM_TEMPLATE:-.aws-sam/build/template.yaml}"
STACK_NAME="${AWS_STACK_NAME:?AWS_STACK_NAME is required}"
REGION="${AWS_REGION:?AWS_REGION is required}"
BUCKET="${AWS_S3_BUCKET:?AWS_S3_BUCKET is required}"

abort_if_stack_managed_rds() {
  local stack="$1" region="$2" ids
  if [[ -z "$stack" || -z "$region" ]]; then
    return 0
  fi
  ids="$(aws cloudformation list-stack-resources \
    --stack-name "$stack" \
    --region "$region" \
    --query "StackResourceSummaries[?ResourceType=='AWS::RDS::DBInstance'].LogicalResourceId" \
    --output text 2>/dev/null || true)"
  if [[ -z "$ids" || "$ids" == "None" ]]; then
    return 0
  fi
  cat >&2 <<EOF
Error: CloudFormation stack '$stack' still contains stack-managed RDS ($ids).

This template no longer creates or updates RDS. An in-place SAM update would try to
destroy that database. Deploy is aborted.

Do this instead (while the old stack is still serving Slack):

  1. Backup from Slack Home → Backup/Restore (needs PRIMARY_WORKSPACE).
     Keep the same DATA_ENCRYPTION_KEY on the new stack.
     See docs/BACKUP_AND_MIGRATION.md and docs/DEPLOY.md.
  2. Delete the CloudFormation app stack. RDS DeletionProtection may block
     delete-stack until you disable protection or delete the instance in the
     RDS console — do that manually after the backup.
  3. Redeploy a fresh stack with AWS_DATABASE_MODE=existing (TiDB / your host)
     or sqlite. Point Slack at the new Function URL / manifest.
  4. Restore the backup JSON on the empty new database.

There is no in-place migrate from stack RDS to TiDB or sqlite.
EOF
  exit 1
}

resolve_aws_database_mode() {
  local mode="${AWS_DATABASE_MODE:-}"
  if [[ "${DATABASE_ENGINE:-}" == "sqlite" ]]; then
    mode="sqlite"
  fi
  mode="${mode:-existing}"
  case "$mode" in
    sqlite|existing) echo "$mode" ;;
    *)
      echo "Error: AWS_DATABASE_MODE must be sqlite or existing (got '$mode')." >&2
      return 1
      ;;
  esac
}

sam_database_engine() {
  case "${1:-mysql}" in
    postgresql) echo postgresql ;;
    *) echo mysql ;;
  esac
}

AWS_DATABASE_MODE="$(resolve_aws_database_mode)"
ENABLE_KEEP_WARM="${ENABLE_KEEP_WARM:-true}"
SAM_DATABASE_ENGINE="$(sam_database_engine "${DATABASE_ENGINE:-mysql}")"

if [[ "$AWS_DATABASE_MODE" == "existing" ]]; then
  if [[ -z "${DATABASE_HOST:-}" ]]; then
    echo "Error: DATABASE_HOST is required when AWS_DATABASE_MODE=existing." >&2
    exit 1
  fi
  if [[ -z "${DATABASE_USER:-}" ]]; then
    echo "Error: DATABASE_USER is required when AWS_DATABASE_MODE=existing." >&2
    exit 1
  fi
  if [[ -z "${DATABASE_PASSWORD:-}" ]]; then
    echo "Error: DATABASE_PASSWORD is required when AWS_DATABASE_MODE=existing." >&2
    exit 1
  fi
else
  DATABASE_HOST=""
  DATABASE_USER="${DATABASE_USER:-}"
  DATABASE_PASSWORD="${DATABASE_PASSWORD:-}"
fi

abort_if_stack_managed_rds "$STACK_NAME" "$REGION"

delete_failed_changesets() {
  local names cs
  names="$(aws cloudformation list-change-sets \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query 'Summaries[?Status==`FAILED`].ChangeSetName' \
    --output text 2>/dev/null || true)"
  [[ -z "$names" || "$names" == "None" ]] && return 0
  for cs in $names; do
    [[ -z "$cs" ]] && continue
    aws cloudformation delete-change-set \
      --change-set-name "$cs" \
      --stack-name "$STACK_NAME" \
      --region "$REGION" 2>/dev/null || true
  done
}

# Lambda may auto-create /aws/lambda/<name> before CloudFormation's LogGroup resource runs,
# causing ResourceExistenceCheck / AlreadyExists on deploy. Delete those so CF can create them.
delete_orphaned_log_groups() {
  local stack="$1" region="$2" functions fn lg_name
  functions="$(aws cloudformation list-stack-resources \
    --stack-name "$stack" \
    --region "$region" \
    --query "StackResourceSummaries[?ResourceType=='AWS::Lambda::Function'].PhysicalResourceId" \
    --output text 2>/dev/null || true)"
  [[ -z "$functions" || "$functions" == "None" ]] && return 0
  for fn in $functions; do
    [[ -z "$fn" ]] && continue
    lg_name="/aws/lambda/${fn}"
    if aws logs describe-log-groups \
      --log-group-name-prefix "$lg_name" \
      --region "$region" \
      --query 'logGroups[].logGroupName' \
      --output text 2>/dev/null | tr '\t' '\n' | grep -Fxq "$lg_name"; then
      echo "=== Deleting orphaned log group: $lg_name ===" >&2
      aws logs delete-log-group --log-group-name "$lg_name" --region "$region" 2>/dev/null || true
    fi
  done
}

params_to_json() {
  python3 -c "
import json, sys
result = []
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    k, _, v = line.partition('=')
    result.append({'ParameterKey': k, 'ParameterValue': v})
print(json.dumps(result))
"
}

# One Key=Value per line (full set for update-stack; sam deploy filters empties).
# Keys must match remaining SAM parameters — do not pass removed RDS/admin/VPC params.
emit_override_lines() {
  printf 'Stage=%s\n' "${STAGE_NAME}"
  printf 'DatabaseMode=%s\n' "${AWS_DATABASE_MODE}"
  printf 'DatabaseEngine=%s\n' "${SAM_DATABASE_ENGINE}"
  printf 'EnableKeepWarm=%s\n' "${ENABLE_KEEP_WARM}"
  printf 'DataEncryptionKey=%s\n' "${DATA_ENCRYPTION_KEY}"
  printf 'DatabasePassword=%s\n' "${DATABASE_PASSWORD:-}"
  printf 'DatabaseUser=%s\n' "${DATABASE_USER:-}"
  printf 'ExistingDatabaseHost=%s\n' "${DATABASE_HOST:-}"
  printf 'ExistingDatabasePort=%s\n' "${DATABASE_PORT:-}"
  printf 'DatabaseSchema=%s\n' "${DATABASE_SCHEMA}"
  printf 'LogLevel=%s\n' "${LOG_LEVEL:-INFO}"
  printf 'RequireAdmin=%s\n' "${REQUIRE_ADMIN:-true}"
  printf 'SoftDeleteRetentionDays=%s\n' "${SOFT_DELETE_RETENTION_DAYS:-30}"
  printf 'SyncbotFederationEnabled=%s\n' "${SYNCBOT_FEDERATION_ENABLED:-false}"
  printf 'SyncbotInstanceId=%s\n' "${SYNCBOT_INSTANCE_ID:-}"
  printf 'SyncbotPublicUrl=%s\n' "${SYNCBOT_PUBLIC_URL:-}"
  printf 'PrimaryWorkspace=%s\n' "${PRIMARY_WORKSPACE:-}"
  printf 'EnableDbReset=%s\n' "${ENABLE_DB_RESET:-}"
  printf 'EnableXRay=%s\n' "${ENABLE_XRAY:-false}"
  printf 'DatabaseTlsEnabled=%s\n' "${DATABASE_TLS_ENABLED:-}"
  printf 'DatabaseSslCaPath=%s\n' "${DATABASE_SSL_CA_PATH:-}"
  printf 'SlackClientID=%s\n' "${SLACK_CLIENT_ID}"
  printf 'SlackClientSecret=%s\n' "${SLACK_CLIENT_SECRET}"
  printf 'SlackSigningSecret=%s\n' "${SLACK_SIGNING_SECRET}"
  printf 'SlackOauthBotScopes=%s\n' "${SLACK_BOT_SCOPES:-app_mentions:read,channels:history,channels:join,channels:read,channels:manage,chat:write,chat:write.customize,files:read,files:write,groups:history,groups:read,groups:write,im:write,reactions:read,reactions:write,team:read,users:read,users:read.email}"
  printf 'SlackOauthUserScopes=%s\n' "${SLACK_USER_SCOPES:-chat:write,channels:history,channels:read,files:read,files:write,groups:history,groups:read,groups:write,im:write,reactions:read,reactions:write,team:read,users:read,users:read.email}"
}

# Single line for sam deploy --parameter-overrides (omit Key= — sam rejects empty values).
build_parameter_overrides() {
  local -a lines=()
  local line
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    [[ "$line" == *"="?* ]] && lines+=("$line")
  done < <(emit_override_lines)
  (IFS=' '; echo "${lines[*]}")
}

deploy_via_update_stack() {
  local packaged template_key template_url cf_params_json

  packaged=".aws-sam/build/packaged-for-update-stack.yaml"
  mkdir -p .aws-sam/build
  echo "=== SAM Package (for CloudFormation update-stack) ==="
  sam package \
    --template-file "$TEMPLATE" \
    --s3-bucket "$BUCKET" \
    --output-template-file "$packaged" \
    --region "$REGION"

  template_key="packaged-templates/${STACK_NAME}-$(date +%s)-$$.yaml"
  echo "=== Upload packaged template to s3://${BUCKET}/${template_key} ==="
  aws s3 cp "$packaged" "s3://${BUCKET}/${template_key}" --region "$REGION"

  template_url="https://${BUCKET}.s3.${REGION}.amazonaws.com/${template_key}"

  cf_params_json="$(emit_override_lines | params_to_json)"

  echo "=== CloudFormation update-stack ==="
  aws cloudformation update-stack \
    --stack-name "$STACK_NAME" \
    --template-url "$template_url" \
    --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
    --region "$REGION" \
    --parameters "$cf_params_json"

  echo "=== Waiting for stack update to complete ==="
  aws cloudformation wait stack-update-complete --stack-name "$STACK_NAME" --region "$REGION"
}

LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT

set +e
set -o pipefail
sam deploy \
  -t "$TEMPLATE" \
  --no-confirm-changeset \
  --no-fail-on-empty-changeset \
  --stack-name "$STACK_NAME" \
  --s3-bucket "$BUCKET" \
  --capabilities CAPABILITY_IAM \
  --region "$REGION" \
  --no-disable-rollback \
  --force-upload \
  --parameter-overrides "$(build_parameter_overrides)" 2>&1 | tee "$LOG"
rc="${PIPESTATUS[0]}"
set +o pipefail
set -e

if [[ "$rc" -eq 0 ]]; then
  exit 0
fi

if grep -q 'EarlyValidation::ResourceExistenceCheck' "$LOG"; then
  echo ""
  echo "=== Changeset rejected by CloudFormation early validation; retrying with direct update-stack... ==="
  delete_failed_changesets || true
  delete_orphaned_log_groups "$STACK_NAME" "$REGION" || true
  deploy_via_update_stack
  exit 0
fi

exit "$rc"
