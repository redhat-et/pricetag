#!/usr/bin/env bash
# Replace the no-traffic EnMaaS application database with an online production
# snapshot. Production is read-only; the target has an automatic rollback copy.

set -euo pipefail

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

command -v oc >/dev/null || die "oc CLI is required"

: "${SOURCE_KUBECONFIG:?Set SOURCE_KUBECONFIG to the production kubeconfig}"
: "${PRODUCTION_CONTEXT:?Set PRODUCTION_CONTEXT to the production oc context}"
: "${PROTECTED_OC_SERVER:?Set PROTECTED_OC_SERVER to the production API server}"
: "${PRICETAG_KUBECONFIG:?Set PRICETAG_KUBECONFIG to the EnMaaS kubeconfig}"
: "${EXPECTED_OC_SERVER:?Set EXPECTED_OC_SERVER to the EnMaaS API server}"
: "${CONFIRM_LIVE_TARGET_RESTORE:?Set CONFIRM_LIVE_TARGET_RESTORE=true for the target replacement}"

PRODUCTION_NAMESPACE="${PRODUCTION_NAMESPACE:-ai-gateway-dogfood}"
TARGET_NAMESPACE="${TARGET_NAMESPACE:-enmaas}"
CLUSTER_NAME="${CLUSTER_NAME:-aigateway-pg}"
ROLLBACK_DATABASE="${ROLLBACK_DATABASE:-aigateway_pre_restore}"
BACKUP_NAME="aigateway-pre-restore-$(date -u +%Y%m%d%H%M%S)"
RESTORE_COMPLETE=false
APPS_SCALED_DOWN=false

[[ "$CONFIRM_LIVE_TARGET_RESTORE" == true ]] || \
  die "set CONFIRM_LIVE_TARGET_RESTORE=true only during the no-traffic target restore"
[[ "$ROLLBACK_DATABASE" =~ ^[a-z][a-z0-9_]{0,62}$ ]] || \
  die "ROLLBACK_DATABASE must be a lowercase PostgreSQL identifier"

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

target_oc apply -f - <<YAML
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata:
  name: ${BACKUP_NAME}
  namespace: ${TARGET_NAMESPACE}
spec:
  cluster:
    name: ${CLUSTER_NAME}
YAML
target_oc wait "backup/${BACKUP_NAME}" \
  --for=jsonpath='{.status.phase}'=completed --timeout=15m

rollback() {
  local exit_code=$?
  if [[ "$RESTORE_COMPLETE" != true && "$APPS_SCALED_DOWN" == true ]]; then
    printf 'Restore failed; rolling back target database from %s\n' "$ROLLBACK_DATABASE" >&2
    target_oc exec "$target_pod" -c postgres -- psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
      -c "select pg_terminate_backend(pid) from pg_stat_activity where datname in ('aigateway', '${ROLLBACK_DATABASE}') and pid <> pg_backend_pid()" >/dev/null || true
    target_oc exec "$target_pod" -c postgres -- psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
      -c "drop database if exists aigateway with (force); alter database ${ROLLBACK_DATABASE} rename to aigateway" || true
  fi

  if [[ "$APPS_SCALED_DOWN" == true ]]; then
    for deployment in maas-api metering-service praxis; do
      target_oc scale "deployment/${deployment}" --replicas=1 >/dev/null || true
    done
  fi
  exit "$exit_code"
}
trap rollback EXIT

for deployment in maas-api metering-service praxis; do
  target_oc scale "deployment/${deployment}" --replicas=0 >/dev/null
done
APPS_SCALED_DOWN=true
for selector in \
  'app.kubernetes.io/name=maas-api' \
  'app=metering-service' \
  'app=praxis'; do
  target_oc wait --for=delete pod -l "$selector" --timeout=180s || true
done

rollback_exists="$(target_oc exec "$target_pod" -c postgres -- psql -U postgres -d postgres -Atqc \
  "select 1 from pg_database where datname='${ROLLBACK_DATABASE}'")"
[[ -z "$rollback_exists" ]] || \
  die "rollback database already exists: $ROLLBACK_DATABASE; refusing to overwrite it"

target_oc exec "$target_pod" -c postgres -- psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c "create database ${ROLLBACK_DATABASE} with template aigateway owner aigateway"

# pg_dump takes a consistent MVCC snapshot while production continues serving traffic.
source_oc exec "$source_pod" -c postgres -- \
  pg_dump -U postgres -d aigateway --format=custom --no-owner --no-privileges |
  target_oc exec -i "$target_pod" -c postgres -- \
    pg_restore -U postgres -d aigateway --clean --if-exists --no-owner --no-privileges --exit-on-error

# pg_restore runs as postgres while the applications run as aigateway.
# Transfer application-object privileges before the apps reconnect and run
# their own migrations; this keeps the database role boundary explicit.
target_oc exec "$target_pod" -c postgres -- psql -U postgres -d aigateway -v ON_ERROR_STOP=1 -c "DO \$\$ DECLARE r record; kind text; BEGIN FOR r IN SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid) AS args FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' LOOP EXECUTE format('ALTER FUNCTION %I.%I(%s) OWNER TO aigateway', r.nspname, r.proname, r.args); END LOOP; END \$\$;" >/dev/null
target_oc exec "$target_pod" -c postgres -- psql -U postgres -d aigateway -v ON_ERROR_STOP=1 -c "DO \$\$ DECLARE r record; kind text; BEGIN FOR r IN SELECT c.relkind, n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r','p','S','v','m','f') LOOP kind := CASE r.relkind WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END; EXECUTE format('ALTER %s %I.%I OWNER TO aigateway', kind, r.nspname, r.relname); END LOOP; END \$\$; GRANT USAGE, CREATE ON SCHEMA public TO aigateway; GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO aigateway; GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO aigateway; GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public TO aigateway; ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO aigateway; ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO aigateway; ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO aigateway;" >/dev/null

RESTORE_COMPLETE=true
for deployment in maas-api metering-service praxis; do
  target_oc scale "deployment/${deployment}" --replicas=1 >/dev/null
done
for deployment in maas-api metering-service praxis; do
  target_oc rollout status "deployment/${deployment}" --timeout=180s
done

count_query="select 'api_keys', count(*) from api_keys union all select 'usage_events', count(*) from usage_events union all select 'usage_hourly', count(*) from usage_hourly union all select 'user_profiles', count(*) from user_profiles order by 1"
target_counts="$(target_oc exec "$target_pod" -c postgres -- psql -U postgres -d aigateway -Atqc "$count_query")"
printf 'Production database restored into the live no-traffic EnMaaS target\nCounts:\n%s\nRollback database retained: %s\n' \
  "$target_counts" "$ROLLBACK_DATABASE"
