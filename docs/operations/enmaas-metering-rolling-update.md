# EnMaaS Metering-only rolling update

Use this runbook to deploy a merged `redhat-et/pricetag-metering` commit
without changing Praxis, MaaS API, Routes, TLS, or NetworkPolicies.

This is the preferred path for a Metering-only change. Do not run the full
`deploy.sh` apply unless its rendered diff has been reviewed. A full apply can
also reconcile the Praxis ConfigMap and roll the gateway.

## 1. Confirm the target

Use the dedicated EnMaaS kubeconfig and verify the current workload state:

```bash
export KUBECONFIG=/secure/path/enmaas-kubeconfig
oc whoami --show-server
oc -n enmaas get deployment maas-api metering-service praxis
oc -n enmaas get deployment/metering-service \
  -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}/{.spec.strategy.rollingUpdate.maxSurge}{"\n"}'
```

The expected Metering state is `4/4` ready replicas and rolling strategy
`0/1`. Stop if the cluster is not healthy before the update.

Run the read-only live validator before changing the image:

```bash
export EXPECTED_OC_SERVER='https://api.<enmaas-cluster>:443'
export PROTECTED_OC_SERVER='https://api.<protected-cluster>:<port>'
export PRICETAG_KUBECONFIG="$KUBECONFIG"
./tools/validate-live-enmaas.sh
```

Before rendering or applying anything, the EnMaaS Metering Deployment must
show this explicit configuration. Do not rely on application defaults:

```text
DASHBOARD_USE_ROLLUPS=true
LIVE_ROLLUPS_ENABLED=false
MONTH_SPEND_ENABLED=false
MONTH_SPEND_WRITE_ENABLED=false
DASHBOARD_CACHE_ENABLED=true
DASHBOARD_CACHE_TTL_SECONDS=60
```

`LIVE_ROLLUPS_ENABLED=false` is deliberate: it keeps the hour-wide rollup
advisory lock out of event ingestion. Dashboard rollup reads are parity-gated
and eventually consistent until the maintenance refresh completes.

Render the target manifests and inspect the actual Deployment before applying:

```bash
oc kustomize deploy/openshift/overlays/enmaas > /tmp/enmaas-rendered.yaml
yq -e '.[] | select(.kind == "Deployment" and .metadata.name == "metering-service") |
  .spec.template.spec.containers[0].env' /tmp/enmaas-rendered.yaml
```

Stop if any required flag is missing or differs from the values above.

## 2. Validate the merged source

Use a full commit SHA from `redhat-et/pricetag-metering` main. Do not build
from a local-only or unpushed commit.

```bash
export METERING_SOURCE_SHA='<40-character-merged-commit-sha>'
git clone https://github.com/redhat-et/pricetag-metering.git /tmp/pricetag-metering
cd /tmp/pricetag-metering
git checkout "$METERING_SOURCE_SHA"
go test ./...
go vet ./...
git diff --check
```

## 3. Build an immutable AMD64 image

The EnMaaS nodes require an AMD64 image. The cluster builder may fail when it
cannot reach public registries, so the proven fallback is a local Podman build.

```bash
export IMAGE_TAG="practice-metering-${METERING_SOURCE_SHA:0:8}"
podman build --platform linux/amd64 \
  -t "localhost/enmaas/metering-service:${IMAGE_TAG}" .
```

## 4. Push to the internal registry

For a macOS Podman machine, forward the OpenShift registry and push from inside
the Podman VM. The VM host address is environment-specific; verify it with
`podman machine ssh 'ip route'` before use.

```bash
export REGISTRY_HOST=192.168.127.254:5001
oc --address=0.0.0.0 -n openshift-image-registry \
  port-forward svc/image-registry 5001:5000
```

In another terminal:

```bash
oc whoami -t | podman machine ssh podman login \
  --tls-verify=false \
  --username="$(oc whoami)" \
  --password-stdin "$REGISTRY_HOST"

podman tag "localhost/enmaas/metering-service:${IMAGE_TAG}" \
  "$REGISTRY_HOST/enmaas/metering-service:${IMAGE_TAG}"

podman machine ssh podman push --tls-verify=false \
  "$REGISTRY_HOST/enmaas/metering-service:${IMAGE_TAG}"
```

Record the immutable digest returned by the cluster:

```bash
export METERING_IMAGE_DIGEST="$(oc -n enmaas get istag \
  "metering-service:${IMAGE_TAG}" \
  -o jsonpath='{.image.dockerImageReference}' | sed 's/.*@//')"
printf '%s\n' "$METERING_IMAGE_DIGEST"
```

Do not deploy a mutable `latest` tag.

## 5. Roll out Metering only

Save the current Deployment for rollback, verify the rolling strategy, and
change only the Metering image:

```bash
export PREVIOUS_IMAGE="$(oc -n enmaas get deployment/metering-service \
  -o jsonpath='{.spec.template.spec.containers[0].image}')"
test -n "$PREVIOUS_IMAGE"
printf 'Previous image: %s\n' "$PREVIOUS_IMAGE"
printf '%s\n' "$PREVIOUS_IMAGE" \
  > "metering-service-previous-image-${IMAGE_TAG}.txt"

oc -n enmaas get deployment/metering-service -o yaml \
  > "metering-service-before-${IMAGE_TAG}.yaml"

test "$(oc -n enmaas get deployment/metering-service \
  -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}/{.spec.strategy.rollingUpdate.maxSurge}')" = "0/1"

oc -n enmaas set image deployment/metering-service \
  "metering-service=image-registry.openshift-image-registry.svc:5000/enmaas/metering-service@${METERING_IMAGE_DIGEST}"

oc -n enmaas rollout status deployment/metering-service --timeout=10m
oc -n enmaas get deployment/metering-service
```

This starts the database migration in the new pods while the old pods remain
serving. Do not restart Praxis or MaaS for a Metering-only change.

The startup migration path takes a shared PostgreSQL advisory lock. During a
rolling update, one pod may migrate while the others wait. Monitor the full
startup window, not only the Deployment rollout result. A pod that logs
`running database migrations` for an extended period, crashes, or reports
`migration lock`/`context canceled` must stop the rollout and be investigated.
See [the migration-lock follow-up](https://github.com/redhat-et/pricetag/pull/81).

## 6. Validate after rollout

Check all pods and recent logs:

```bash
oc -n enmaas get deployment maas-api metering-service praxis
oc -n enmaas get pods -l app=metering-service
oc -n enmaas logs -l app=metering-service --since=10m \
  | grep -Ei 'error|fatal|panic|migration' || true

oc -n enmaas get pods -l app=metering-service \
  -o custom-columns='NAME:.metadata.name,READY:.status.containerStatuses[0].ready,RESTARTS:.status.containerStatuses[0].restartCount'
```

Check the rollup state before calling the rollout successful:

```bash
oc -n enmaas get deployment/metering-service \
  -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' \
  | grep -E 'DASHBOARD_USE_ROLLUPS|LIVE_ROLLUPS_ENABLED|MONTH_SPEND|DASHBOARD_CACHE'
```

The protected `/api/v1/admin/rollups` status must report:

```text
rollups_ready=true
parity_healthy=true
serving_rollups=true
```

If parity is red, keep `DASHBOARD_USE_ROLLUPS=false`. A red result caused by
the in-flight hour is a code/configuration defect: the parity window must
exclude the current hour when live rollup writes are disabled. Do not manually
force rollup reads on while parity is red.

## 7.1 Read-replica/tool-rollup grant

If EnMaaS uses the readonly database role, it must have SELECT access to every
rollup table the dashboard reads:

```sql
GRANT SELECT ON usage_events TO metering_reader;
GRANT SELECT ON model_pricing TO metering_reader;
GRANT SELECT ON user_profiles TO metering_reader;
GRANT SELECT ON usage_hourly TO metering_reader;
GRANT SELECT ON usage_tool_hourly TO metering_reader;
```

Apply these grants **after** the Metering startup migration has created
`usage_hourly` and `usage_tool_hourly`. On RDS, use the approved RDS database
administration process; do not use the CNPG `oc exec` command. On CNPG, the
guarded deployment applies the grant after the table exists.

Do not treat a successful Pod readiness check as proof of readonly-role
permissions. Run a read-only privilege check using the actual
`READ_DATABASE_URL` role before enabling rollup reads.

Run the read-only smoke and authentication checks:

```bash
PATH=/opt/homebrew/opt/coreutils/libexec/gnubin:$PATH \
  ./tools/functional-test.sh --target enmaas --level smoke
PATH=/opt/homebrew/opt/coreutils/libexec/gnubin:$PATH \
  ./tools/functional-test.sh --target enmaas --level auth
```

The authentication test can report catalog expectation failures that are
unrelated to a Metering-only image. Record them separately. Use a valid Partner
API token to verify the authenticated user API when needed; never put tokens in
command arguments or logs.

Inference validation is optional and consumes tokens. Run it only after explicit
approval:

```bash
MODEL_FREE='rits/zai-org/glm-5-3' \
MODEL_OPENAI='gpt-5.6-luna' \
  PATH=/opt/homebrew/opt/coreutils/libexec/gnubin:$PATH \
  ./tools/functional-test.sh --target enmaas --level inference --confirm-prod
```

## 8. Rollback

If readiness, API health, or error rates regress, restore the previous recorded
image. This does not change Praxis or MaaS. Use the same shell's
`PREVIOUS_IMAGE`, or load the saved value in a new shell:

```bash
export PREVIOUS_IMAGE="$(cat "metering-service-previous-image-${IMAGE_TAG}.txt")"
test -n "$PREVIOUS_IMAGE"
oc -n enmaas set image deployment/metering-service \
  "metering-service=${PREVIOUS_IMAGE}"
oc -n enmaas rollout status deployment/metering-service --timeout=10m
```

Record the source SHA, image digest, previous digest, rollout time, validation
results, and any warnings in the deployment evidence.

## 9. MTD accumulator activation (separate change)

The rolling image update does **not** activate the monthly spend accumulator.
Keep these flags false during the normal update:

```text
MONTH_SPEND_ENABLED=false
MONTH_SPEND_WRITE_ENABLED=false
```

Only activate MTD in a separate approved operation:

1. Ensure every Metering pod runs the accumulator-capable image.
2. Drain/stop Metering event ingestion; inference can remain available but
   Metering fail-open behavior may apply during the drain.
3. Run `scripts/backfill-month-spend.sql` from an approved writer connection.
4. The script fails closed on NULL historical `cost_usd` and snapshots only
   after ingestion is drained.
5. Validate dollars, tokens, requests, and quota-denial counts.
6. Enable both flags together and wait for all pods to become Ready:

```text
MONTH_SPEND_WRITE_ENABLED=true
MONTH_SPEND_ENABLED=true
```

7. Reopen ingestion and verify first-request totals, duplicate-event
   idempotency, and quota decisions.
