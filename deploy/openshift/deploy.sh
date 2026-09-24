#!/usr/bin/env bash
# Idempotently deploy a PriceTag profile to OpenShift.
#
# Existing Secrets are preserved on repeat runs. Set ROTATE_SECRETS=true only
# when an intentional credential rotation is planned.

set -euo pipefail

NAMESPACE="${NAMESPACE:-ai-gateway-dogfood}"
PROFILE="${PROFILE:-dogfood}"
STORAGE_CLASS="${STORAGE_CLASS:-ibmc-vpc-block-10iops-tier}"
ROTATE_SECRETS="${ROTATE_SECRETS:-false}"
UPDATE_CONFIG="${UPDATE_CONFIG:-false}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROFILE_DIR="$SCRIPT_DIR/overlays/$PROFILE"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

oc whoami >/dev/null 2>&1 || die "not logged in to OpenShift"
command -v envsubst >/dev/null || die "envsubst not found"
[[ -d "$PROFILE_DIR" ]] || die "unknown profile: $PROFILE"

case "$PROFILE" in
  dogfood) expected_namespace=ai-gateway-dogfood ;;
  test) expected_namespace=pricetag-test ;;
  *) die "unsupported profile: $PROFILE" ;;
esac
[[ "$NAMESPACE" == "$expected_namespace" ]] || \
  die "PROFILE=$PROFILE requires NAMESPACE=$expected_namespace"

secret_exists() { oc -n "$NAMESPACE" get secret "$1" >/dev/null 2>&1; }
config_exists() { oc -n "$NAMESPACE" get configmap "$1" >/dev/null 2>&1; }

oc apply -f "$PROFILE_DIR/namespace.yaml"

# The database password is generated once and then read back on every rerun.
if secret_exists aigateway-db-app; then
  PG_PASSWORD="$(oc -n "$NAMESPACE" get secret aigateway-db-app \
    -o jsonpath='{.data.password}' | base64 --decode)"
  [[ -n "$PG_PASSWORD" ]] || die "aigateway-db-app has no password"
else
  PG_PASSWORD="${PG_PASSWORD:-$(openssl rand -hex 32)}"
  oc -n "$NAMESPACE" create secret generic aigateway-db-app \
    --from-literal=username=aigateway \
    --from-literal=password="$PG_PASSWORD" \
    --dry-run=client -o yaml | oc apply -f -
fi

# These derived connection secrets are safe to reconcile on every run.
oc -n "$NAMESPACE" create secret generic postgresql-credentials \
  --from-literal=POSTGRES_USER=aigateway \
  --from-literal=POSTGRES_PASSWORD="$PG_PASSWORD" \
  --from-literal=POSTGRES_DB=aigateway \
  --from-literal=MAAS_DB_URL="postgresql://aigateway:${PG_PASSWORD}@aigateway-pg-rw:5432/aigateway?sslmode=disable" \
  --from-literal=METERING_DB_URL="postgresql://aigateway:${PG_PASSWORD}@aigateway-pg-rw:5432/aigateway?sslmode=disable" \
  --dry-run=client -o yaml | oc apply -f -

oc -n "$NAMESPACE" create secret generic maas-db-config \
  --from-literal=DB_CONNECTION_URL="postgresql://aigateway:${PG_PASSWORD}@aigateway-pg-rw:5432/aigateway?sslmode=disable" \
  --dry-run=client -o yaml | oc apply -f -

if ! secret_exists provider-credentials || [[ "$ROTATE_SECRETS" == true ]]; then
  for name in ANTHROPIC_API_KEY OPENAI_API_KEY LITELLM_API_KEY CB_LITELLM_API_KEY; do
    [[ -n "${!name:-}" ]] || die "$name is required to create provider-credentials"
  done
  oc -n "$NAMESPACE" create secret generic provider-credentials \
    --from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
    --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY" \
    --from-literal=LITELLM_API_KEY="$LITELLM_API_KEY" \
    --from-literal=CB_LITELLM_API_KEY="$CB_LITELLM_API_KEY" \
    --dry-run=client -o yaml | oc apply -f -
fi

if ! secret_exists cnpg-backup-cos || [[ "$ROTATE_SECRETS" == true ]]; then
  for name in COS_ACCESS_KEY_ID COS_SECRET_ACCESS_KEY; do
    [[ -n "${!name:-}" ]] || die "$name is required to create cnpg-backup-cos"
  done
  oc -n "$NAMESPACE" create secret generic cnpg-backup-cos \
    --from-literal=ACCESS_KEY_ID="$COS_ACCESS_KEY_ID" \
    --from-literal=SECRET_ACCESS_KEY="$COS_SECRET_ACCESS_KEY" \
    --dry-run=client -o yaml | oc apply -f -
fi

if ! config_exists pricetag-config || [[ "$UPDATE_CONFIG" == true ]]; then
  for name in ADMIN_USERS SUPERADMIN_USERS MAAS_SECURE MAAS_DEBUG_MODE \
    QWEN_ENDPOINT CB_GLM_ENDPOINT; do
    [[ -n "${!name:-}" ]] || die "$name is required to create pricetag-config"
  done
  oc -n "$NAMESPACE" create configmap pricetag-config \
    --from-literal=ADMIN_USERS="$ADMIN_USERS" \
    --from-literal=SUPERADMIN_USERS="$SUPERADMIN_USERS" \
    --from-literal=MAAS_SECURE="$MAAS_SECURE" \
    --from-literal=MAAS_DEBUG_MODE="$MAAS_DEBUG_MODE" \
    --dry-run=client -o yaml | oc apply -f -
fi

if ! secret_exists pricetag-session || [[ "$ROTATE_SECRETS" == true ]]; then
  SESSION_SECRET="${SESSION_SECRET:-$(openssl rand -hex 32)}"
  oc -n "$NAMESPACE" create secret generic pricetag-session \
    --from-literal=SESSION_SECRET="$SESSION_SECRET" \
    --dry-run=client -o yaml | oc apply -f -
fi

# Cluster-scoped CRDs and the pinned CNPG operator are apply-safe. The operator
# is installed once per cluster; the namespaced Cluster is safe to reconcile.
for crd in "$SCRIPT_DIR"/crds/*.yaml; do
  oc apply -f "$crd"
done
oc apply -f "$SCRIPT_DIR/database/cnpg/cnpg-operator-1.30.0.yaml"
oc -n cnpg-system rollout status deploy/cnpg-controller-manager --timeout=300s

[[ -n "${COS_BUCKET:-}" && -n "${COS_ENDPOINT:-}" ]] || \
  die "COS_BUCKET and COS_ENDPOINT are required for the CNPG profile"
export NAMESPACE STORAGE_CLASS COS_BUCKET COS_ENDPOINT
envsubst '${NAMESPACE} ${STORAGE_CLASS} ${COS_BUCKET} ${COS_ENDPOINT}' \
  < "$SCRIPT_DIR/database/cnpg/10-cluster.yaml" | oc apply -f -
envsubst '${NAMESPACE}' \
  < "$SCRIPT_DIR/database/cnpg/20-scheduled-backup.yaml" | oc apply -f -
oc -n "$NAMESPACE" wait cluster/aigateway-pg --for=condition=Ready --timeout=10m

oc kustomize "$PROFILE_DIR" |
  envsubst '${NAMESPACE} ${QWEN_ENDPOINT} ${CB_GLM_ENDPOINT}' | oc apply -f -

oc -n "$NAMESPACE" rollout status deployment/maas-api --timeout=180s
oc -n "$NAMESPACE" rollout status deployment/metering-service --timeout=180s
oc -n "$NAMESPACE" rollout status deployment/praxis --timeout=180s

printf '\nPriceTag deployed to %s (%s)\n' "$NAMESPACE" "$PROFILE"
oc -n "$NAMESPACE" get pods
