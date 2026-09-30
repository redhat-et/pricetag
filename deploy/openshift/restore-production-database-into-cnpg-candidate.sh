#!/usr/bin/env bash
# Restore an online production snapshot into a new, non-active CNPG database.
# EnMaaS applications use RDS, so this is a no-outage rollback-target rehearsal;
# it never changes the active CNPG database or application connection Secrets.

set -euo pipefail

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

command -v oc >/dev/null || die "oc CLI is required"

: "${SOURCE_KUBECONFIG:?Set SOURCE_KUBECONFIG to the production kubeconfig}"
: "${PRODUCTION_CONTEXT:?Set PRODUCTION_CONTEXT to the production oc context}"
: "${PROTECTED_OC_SERVER:?Set PROTECTED_OC_SERVER to the production API server}"
: "${PRICETAG_KUBECONFIG:?Set PRICETAG_KUBECONFIG to the EnMaaS kubeconfig}"
: "${EXPECTED_OC_SERVER:?Set EXPECTED_OC_SERVER to the EnMaaS API server}"
: "${CONFIRM_CNPG_RESTORE_REHEARSAL:?Set CONFIRM_CNPG_RESTORE_REHEARSAL=true for the isolated rehearsal}"

PRODUCTION_NAMESPACE="${PRODUCTION_NAMESPACE:-ai-gateway-dogfood}"
TARGET_NAMESPACE="${TARGET_NAMESPACE:-enmaas}"
CLUSTER_NAME="${CLUSTER_NAME:-aigateway-pg}"
TARGET_DATABASE="${TARGET_DATABASE:-aigateway_restore_candidate}"
TARGET_CREATED=false
RESTORE_COMPLETE=false

[[ "$CONFIRM_CNPG_RESTORE_REHEARSAL" == true ]] || \
  die "set CONFIRM_CNPG_RESTORE_REHEARSAL=true only for the isolated rehearsal"
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
[[ "$target_server" != "$PROTECTED_OC_SERVER" ]] || \
  die "refusing to restore into the protected production server"

source_pod="$(source_oc get cluster "$CLUSTER_NAME" -o jsonpath='{.status.currentPrimary}')"
target_pod="$(target_oc get cluster "$CLUSTER_NAME" -o jsonpath='{.status.currentPrimary}')"
[[ -n "$source_pod" && -n "$target_pod" ]] || die "could not resolve CNPG primary pods"

cleanup() {
  local exit_code=$?
  if [[ "$RESTORE_COMPLETE" != true && "$TARGET_CREATED" == true ]]; then
    printf 'Restore failed; removing candidate database %s\n' "$TARGET_DATABASE" >&2
    target_oc exec "$target_pod" -c postgres -- psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
      -c "select pg_terminate_backend(pid) from pg_stat_activity where datname='${TARGET_DATABASE}' and pid <> pg_backend_pid()" >/dev/null || true
    target_oc exec "$target_pod" -c postgres -- psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
      -c "drop database if exists ${TARGET_DATABASE} with (force)" >/dev/null || true
  fi
  exit "$exit_code"
}
trap cleanup EXIT

count_query="select 'api_keys', count(*) from api_keys union all select 'usage_events', count(*) from usage_events union all select 'usage_hourly', count(*) from usage_hourly union all select 'user_profiles', count(*) from user_profiles order by 1"
source_counts_before="$(source_oc exec "$source_pod" -c postgres -- psql -U postgres -d aigateway -Atqc "$count_query")"

target_exists="$(target_oc exec "$target_pod" -c postgres -- psql -U postgres -d postgres -Atqc \
  "select 1 from pg_database where datname='${TARGET_DATABASE}'")"
[[ -z "$target_exists" ]] || \
  die "candidate database already exists: $TARGET_DATABASE; refusing to overwrite it"

target_oc exec "$target_pod" -c postgres -- psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c "create database ${TARGET_DATABASE} owner aigateway"
TARGET_CREATED=true

# pg_dump takes a consistent MVCC snapshot while production continues serving traffic.
source_oc exec "$source_pod" -c postgres -- \
  pg_dump -U postgres -d aigateway --format=custom --no-owner --no-privileges |
  target_oc exec -i "$target_pod" -c postgres -- \
    pg_restore -U postgres -d "$TARGET_DATABASE" --no-owner --no-privileges --exit-on-error

source_counts_after="$(source_oc exec "$source_pod" -c postgres -- psql -U postgres -d aigateway -Atqc "$count_query")"
target_counts="$(target_oc exec "$target_pod" -c postgres -- psql -U postgres -d "$TARGET_DATABASE" -Atqc "$count_query")"
RESTORE_COMPLETE=true
printf 'Production database restored into an isolated CNPG candidate\nSource counts before online dump:\n%s\nSource counts after online dump:\n%s\nCandidate counts:\n%s\nCandidate database retained for inspection: %s\n' \
  "$source_counts_before" "$source_counts_after" "$target_counts" "$TARGET_DATABASE"
