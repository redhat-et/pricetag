#!/usr/bin/env bash
# Deploy the PriceTag core stack to OpenShift.
#
# Prerequisites:
#   - oc login to your cluster
#   - Component images available in the registry configured by the manifests
#   - provider keys, COS credentials, admin users, and upstream endpoints set
#
# Usage:
#   export ANTHROPIC_API_KEY="sk-ant-..."
#   export LITELLM_API_KEY="sk-..."
#   ./deploy/openshift/deploy.sh

set -euo pipefail

NAMESPACE="${NAMESPACE:-ai-gateway-dogfood}"
PROFILE="${PROFILE:-dogfood}"
STORAGE_CLASS="${STORAGE_CLASS:-ibmc-vpc-block-10iops-tier}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$SCRIPT_DIR/base"
PROFILE_DIR="$SCRIPT_DIR/overlays/$PROFILE"

# ── Preflight ─────────────────────────────────────────────────

if ! oc whoami > /dev/null 2>&1; then
    echo "ERROR: not logged in to OpenShift. Run: oc login ..."
    exit 1
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "ERROR: ANTHROPIC_API_KEY not set."
    echo "  export ANTHROPIC_API_KEY=\"sk-ant-...\""
    exit 1
fi

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    echo "ERROR: OPENAI_API_KEY not set."
    echo "  export OPENAI_API_KEY=\"sk-...\""
    exit 1
fi

if [[ -z "${LITELLM_API_KEY:-}" ]]; then
    echo "ERROR: LITELLM_API_KEY not set."
    echo "  export LITELLM_API_KEY=\"sk-...\""
    exit 1
fi

for required in ANTHROPIC_API_KEY OPENAI_API_KEY LITELLM_API_KEY \
    CB_LITELLM_API_KEY COS_ACCESS_KEY_ID COS_SECRET_ACCESS_KEY COS_BUCKET \
    COS_ENDPOINT ADMIN_USERS SUPERADMIN_USERS QWEN_ENDPOINT CB_GLM_ENDPOINT; do
    if [[ -z "${!required:-}" ]]; then
        echo "ERROR: $required not set."
        exit 1
    fi
done

command -v envsubst >/dev/null || { echo "ERROR: envsubst not found"; exit 1; }
SESSION_SECRET="${SESSION_SECRET:-$(openssl rand -hex 32)}"

if [[ ! -d "$PROFILE_DIR" ]]; then
    echo "ERROR: unknown profile: $PROFILE"
    exit 1
fi

echo "Deploying to: $(oc whoami --show-server)"
echo "Namespace:    $NAMESPACE"
echo ""

# ── Generate or reuse database password ─────────────────────────

if oc -n "$NAMESPACE" get secret aigateway-db-app >/dev/null 2>&1; then
    PG_PASSWORD=$(oc -n "$NAMESPACE" get secret aigateway-db-app -o jsonpath='{.data.password}' | base64 --decode)
else
    PG_PASSWORD=$(openssl rand -hex 32)
fi

# ── Create namespace ──────────────────────────────────────────

oc apply -f "$PROFILE_DIR/namespace.yaml"

# ── Create secrets with real values ───────────────────────────

oc -n "$NAMESPACE" create secret generic aigateway-db-app \
    --from-literal=username=aigateway \
    --from-literal=password="$PG_PASSWORD" \
    --dry-run=client -o yaml | oc apply -f -

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

oc -n "$NAMESPACE" create secret generic cnpg-backup-cos \
    --from-literal=ACCESS_KEY_ID="$COS_ACCESS_KEY_ID" \
    --from-literal=SECRET_ACCESS_KEY="$COS_SECRET_ACCESS_KEY" \
    --dry-run=client -o yaml | oc apply -f -

oc -n "$NAMESPACE" create secret generic provider-credentials \
    --from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
    --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY" \
    --from-literal=LITELLM_API_KEY="$LITELLM_API_KEY" \
    --from-literal=CB_LITELLM_API_KEY="$CB_LITELLM_API_KEY" \
    --dry-run=client -o yaml | oc apply -f -

oc -n "$NAMESPACE" create configmap pricetag-config \
    --from-literal=ADMIN_USERS="$ADMIN_USERS" \
    --from-literal=SUPERADMIN_USERS="$SUPERADMIN_USERS" \
    --dry-run=client -o yaml | oc apply -f -

oc -n "$NAMESPACE" create secret generic pricetag-session \
    --from-literal=SESSION_SECRET="$SESSION_SECRET" \
    --dry-run=client -o yaml | oc apply -f -

# ── Deploy CNPG and the application profile ───────────────────

echo "Deploying CloudNativePG..."
oc apply -f "$SCRIPT_DIR/database/cnpg/cnpg-operator-1.30.0.yaml"
oc -n cnpg-system rollout status deploy/cnpg-controller-manager --timeout=300s

export NAMESPACE STORAGE_CLASS COS_BUCKET COS_ENDPOINT
envsubst '${NAMESPACE} ${STORAGE_CLASS} ${COS_BUCKET} ${COS_ENDPOINT}' \
    < "$SCRIPT_DIR/database/cnpg/10-cluster.yaml" | oc apply -f -
envsubst '${NAMESPACE}' \
    < "$SCRIPT_DIR/database/cnpg/20-scheduled-backup.yaml" | oc apply -f -
oc -n "$NAMESPACE" wait cluster/aigateway-pg --for=condition=Ready --timeout=10m

echo "Deploying PriceTag profile: $PROFILE"
oc kustomize "$PROFILE_DIR" | \
    envsubst '${NAMESPACE} ${QWEN_ENDPOINT} ${CB_GLM_ENDPOINT}' | oc apply -f -
oc -n "$NAMESPACE" rollout status deployment/maas-api --timeout=180s
oc -n "$NAMESPACE" rollout status deployment/metering-service --timeout=180s
oc -n "$NAMESPACE" rollout status deployment/praxis --timeout=180s

# ── Print connection info ─────────────────────────────────────

ANTHROPIC_ROUTE=$(oc -n "$NAMESPACE" get route ai-gateway-anthropic -o jsonpath='{.spec.host}')
OPENAI_ROUTE=$(oc -n "$NAMESPACE" get route ai-gateway-openai -o jsonpath='{.spec.host}')
DASHBOARD_URL=$(oc -n "$NAMESPACE" get route dashboard -o jsonpath='{.spec.host}')

echo ""
echo "=========================================="
echo "  AI Gateway Dogfood — Deployed"
echo "=========================================="
echo ""
echo "  Anthropic: https://$ANTHROPIC_ROUTE"
echo "  OpenAI:    https://$OPENAI_ROUTE"
echo "  Dashboard: https://$DASHBOARD_URL/dashboard"
echo ""
echo "  Connect Claude Code:"
echo "    ANTHROPIC_BASE_URL=https://$ANTHROPIC_ROUTE claude"
echo ""
echo "  Connect OpenAI SDK:"
echo "    OPENAI_BASE_URL=https://$OPENAI_ROUTE/v1 python your-script.py"
echo ""
echo "  Pods:"
oc -n "$NAMESPACE" get pods
echo ""
