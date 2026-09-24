#!/usr/bin/env bash
# Stream an online production PostgreSQL dump into an isolated EnMaaS database.
# This script never writes to production and never replaces the live target DB.

set -euo pipefail

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

command -v oc >/dev/null || die "oc CLI is required"

: "${SOURCE_KUBECONFIG:?Set SOURCE_KUBECONFIG to the production kubeconfig}"
: "${PRODUCTION_CONTEXT:?Set PRODUCTION_CONTEXT to the production oc context}"
: "${PROTECTED_OC_SERVER:?Set PROTECTED_OC_SERVER to the production API server}"
: "${PRICETAG_KUBECONFIG:?Set PRICETAG_KUBECONFIG to the EnMaaS kubeconfig}"
: "${EXPECTED_OC_SERVER:?Set EXPECTED_OC_SERVER to the EnMaaS API server}"
: "${CONFIRM_MIGRATION_TARGET:?Set CONFIRM_MIGRATION_TARGET=true for the isolated target DB}"

PRODUCTION_NAMESPACE="${PRODUCTION_NAMESPACE:-ai-gateway-dogfood}"
TARGET_NAMESPACE="${TARGET_NAMESPACE:-enmaas}"
CLUSTER_NAME="${CLUSTER_NAME:-aigateway-pg}"
TARGET_DATABASE="${TARGET_DATABASE:-aigateway_migration}"

[[ "$CONFIRM_MIGRATION_TARGET" == true ]] || \
  die "set CONFIRM_MIGRATION_TARGET=true only for the isolated target database"
[[ "$TARGET_DATABASE" =~ ^[a-z][a-z0-9_]{0,62}$ ]] || \
  die "TARGET_DATABASE must be a lowercase PostgreSQL identifier"

source_oc() {
  oc --kubeconfig "$SOURCE_KUBECONFIG" --context "$PRODUCTION_CONTEXT" \
    -n "$PRODUCTION_NAMESPACE" "$@"
}

target_oc() {
  oc --kubeconfig "$PRICETAG_KUBECONFIG" -n "$TARGET_NAMESPACE" "$@"
}

source_server="$(source_oc whoami --show-server)"
[[ "$source_server" == "$PROTECTED_OC_SERVER" ]] || \
  die "source server is $source_server, expected the protected production server"
target_server="$(target_oc whoami --show-server)"
[[ "$target_server" == "$EXPECTED_OC_SERVER" ]] || \
  die "target server is $target_server, expected the EnMaaS server"

source_pod="$(source_oc get cluster "$CLUSTER_NAME" -o jsonpath='{.status.currentPrimary}')"
target_pod="$(target_oc get cluster "$CLUSTER_NAME" -o jsonpath='{.status.currentPrimary}')"
[[ -n "$source_pod" && -n "$target_pod" ]] || die "could not resolve CNPG primary pods"

count_query="select 'api_keys', count(*) from api_keys union all select 'usage_events', count(*) from usage_events union all select 'usage_hourly', count(*) from usage_hourly union all select 'user_profiles', count(*) from user_profiles order by 1"
source_counts_before="$(source_oc exec "$source_pod" -c postgres -- psql -U postgres -d aigateway -Atqc "$count_query")"

target_exists="$(target_oc exec "$target_pod" -c postgres -- \
  psql -U postgres -d postgres -Atqc \
  "select 1 from pg_database where datname='${TARGET_DATABASE}'")"
[[ -z "$target_exists" ]] || \
  die "target database already exists: $TARGET_DATABASE; refusing to overwrite it"

target_oc exec "$target_pod" -c postgres -- \
  psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c "create database ${TARGET_DATABASE}"

# pg_dump takes a consistent MVCC snapshot while production remains online.
source_oc exec "$source_pod" -c postgres -- \
  pg_dump -U postgres -d aigateway --format=custom --no-owner --no-privileges |
  target_oc exec -i "$target_pod" -c postgres -- \
    pg_restore -U postgres -d "$TARGET_DATABASE" --no-owner --no-privileges --exit-on-error

source_counts_after="$(source_oc exec "$source_pod" -c postgres -- psql -U postgres -d aigateway -Atqc "$count_query")"
target_counts="$(target_oc exec "$target_pod" -c postgres -- psql -U postgres -d "$TARGET_DATABASE" -Atqc "$count_query")"

printf 'Source counts before online dump:\n%s\n' "$source_counts_before"
printf 'Source counts after online dump:\n%s\n' "$source_counts_after"
printf 'Isolated target counts:\n%s\n' "$target_counts"
printf 'Note: source rows may increase during an online dump; target reflects the consistent dump snapshot.\n'

printf 'Isolated migration restore completed\nSource: %s/%s\nTarget: %s/%s\nDatabase: %s\n' \
  "$PRODUCTION_NAMESPACE" "$CLUSTER_NAME" "$TARGET_NAMESPACE" "$CLUSTER_NAME" \
  "$TARGET_DATABASE"
