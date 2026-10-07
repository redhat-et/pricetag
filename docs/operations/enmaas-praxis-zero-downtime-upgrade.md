# EnMaaS Praxis zero-downtime upgrade

Use this runbook for a Praxis code or configuration upgrade in the live
`enmaas` namespace. It is different from a Metering-only rollout because
Praxis carries all inference traffic.

This runbook uses the current EnMaaS deployment model:

- four Praxis replicas;
- `RollingUpdate` with `maxUnavailable=0` and `maxSurge=1`;
- OpenShift Routes and HAProxy remain the public edge;
- images are pinned by digest;
- no scale-to-zero operation.

The current repository does not contain the old `deploy/openshift/shadow.sh`
workflow. Do not copy the dogfood shadow commands into EnMaaS. Use an approved
stage or shadow environment for deep behavior testing before this runbook.

`maxUnavailable=0` prevents a planned reduction in Ready replicas. It does not
prove that a new pod handles inference correctly, nor does it prevent the new
pod from receiving live traffic as soon as its readiness probe passes. This
runbook is therefore a compatible-release rolling procedure, not a complete
canary procedure.

The current Praxis readiness probe checks the local `/healthy` endpoint. It does
not independently prove every inference listener, stream, tool path, or
metering path. The current deployment also uses a 30-second termination grace
period and has no documented stream-drain hook. Do not claim uninterrupted
long-lived streams until graceful drain is tested for the target Praxis build.

## 1. Decide the change type

### Binary-only change

Use the direct image rollout in Section 5 when the live `praxis-config`
ConfigMap does not change. This is the normal path for a validated Praxis image.

### Binary plus configuration change

Use the guarded full deployment path after reviewing the rendered diff. The
Praxis ConfigMap is read at pod startup. A ConfigMap update alone does not
change running pipelines. The deployment checksum patch triggers a safe rolling
restart for EnMaaS.

Do not run the full deployment script only to update a Praxis image if its diff
would reconcile unrelated Metering, MaaS, Route, or Secret changes. Use the
direct image path instead.

### Major behavior change

For a major core, filter, protocol, routing, or streaming change, first use an
approved stage or shadow environment. Test native Anthropic, OpenAI, streaming,
tool, authentication, and metering behavior before production.

The current EnMaaS namespace has no shadow Deployment and no weighted alternate
Route backend. Do not invent Route weights during an upgrade. If a production
canary is required, add and review that capability separately before the
upgrade.

## 2. Preflight the live target

Use the dedicated kubeconfig. Record the current image and ConfigMap before any
write:

```bash
export KUBECONFIG=/secure/path/enmaas-kubeconfig
export EXPECTED_OC_SERVER='https://api.enmaas-prod.<cluster>:443'
export NAMESPACE=enmaas

oc whoami --show-server
oc -n "$NAMESPACE" get deployment/praxis \
  -o jsonpath='replicas={.spec.replicas} ready={.status.readyReplicas} available={.status.availableReplicas} strategy={.spec.strategy.type} maxUnavailable={.spec.strategy.rollingUpdate.maxUnavailable} maxSurge={.spec.strategy.rollingUpdate.maxSurge} image={.spec.template.spec.containers[0].image}{"\n"}'

export PREVIOUS_IMAGE="$(oc -n "$NAMESPACE" get deployment/praxis \
  -o jsonpath='{.spec.template.spec.containers[0].image}')"
test -n "$PREVIOUS_IMAGE"
if [[ ! "$PREVIOUS_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]]; then
  echo "previous Praxis image is not digest-pinned: $PREVIOUS_IMAGE" >&2
  exit 1
fi
printf 'Previous image: %s\n' "$PREVIOUS_IMAGE"
printf '%s\n' "$PREVIOUS_IMAGE" > praxis-previous-image.txt

oc -n "$NAMESPACE" get deployment/praxis -o yaml > praxis-before.yaml
oc -n "$NAMESPACE" get configmap/praxis-config -o yaml > praxis-config-before.yaml
oc -n "$NAMESPACE" get route -l app=praxis -o yaml > praxis-routes-before.yaml
```

Stop unless the deployment is fully ready and the strategy is `0/1`. Confirm
that all required Routes are admitted. Do not change Route weights during a
binary-only rollout. Also record the termination grace period and confirm that
the expected longest stream can drain within it. If that drain behavior is not
proven, stop and use a tested stage or planned maintenance window.

## 3. Validate the source and live configuration

Use a pushed, reviewed full commit SHA from `redhat-et/praxis-ai`. Do not build
from an unpushed worktree:

```bash
export PRAXIS_SOURCE_SHA='<40-character-merged-commit-sha>'
git clone https://github.com/redhat-et/praxis-ai.git /tmp/praxis-ai-upgrade
cd /tmp/praxis-ai-upgrade
git checkout "$PRAXIS_SOURCE_SHA"

cargo test -p praxis-ai-filters
cargo check --workspace --all-targets
make lint
```

Extract the live configuration and validate it with the new binary. This must
pass before the image is adopted:

```bash
oc -n enmaas get configmap/praxis-config \
  -o jsonpath='{.data.praxis\.yaml}' > /tmp/praxis-config-live.yaml
cargo run -p praxis-ai-proxy -- \
  --config /tmp/praxis-config-live.yaml --validate
```

If the upgrade changes configuration, create a reviewed new file and compare
it with the saved live file. Check filter defaults, model catalog entries,
metering settings, private-target permissions, buffer limits, provider routes,
and credential-injection settings. Never overwrite live configuration from an
old committed template without a diff.

## 4. Build and record an immutable image

The approved EnMaaS builder fetches the exact pushed source commit. It does not
roll Praxis:

```bash
cd /path/to/pricetag
export NAMESPACE=enmaas
export PRAXIS_SOURCE_SHA='<40-character-merged-commit-sha>'
export PRAXIS_AI_FEATURES=full,gcp-adc-filter
export PRAXIS_SOURCE_REPO=https://github.com/redhat-et/praxis-ai.git

./deploy/openshift/build-praxis-et.sh

export PRAXIS_IMAGE_TAG="practice-${PRAXIS_SOURCE_SHA:0:8}"
export PRAXIS_IMAGE_DIGEST="$(oc -n enmaas get istag \
  "praxis-ai:${PRAXIS_IMAGE_TAG}" \
  -o jsonpath='{.image.dockerImageReference}' | sed 's/.*@//')"
test "$PRAXIS_IMAGE_DIGEST" = sha256:*
printf 'Source: %s\nImage: %s\n' "$PRAXIS_SOURCE_SHA" "$PRAXIS_IMAGE_DIGEST"
```

Keep the source SHA, digest, build log, and validation result in the upgrade
evidence. Do not deploy a mutable `latest` tag.

## 5. Roll out a binary-only change

The old pods remain available while each new pod becomes Ready. The command
changes only Praxis:

```bash
export NEW_IMAGE="image-registry.openshift-image-registry.svc:5000/enmaas/praxis-ai@${PRAXIS_IMAGE_DIGEST}"

: "${PRICETAG_KUBECONFIG:?Set PRICETAG_KUBECONFIG to the dedicated EnMaaS kubeconfig}"
: "${EXPECTED_OC_SERVER:?Set EXPECTED_OC_SERVER to the EnMaaS API server}"
: "${PROTECTED_OC_SERVER:?Set PROTECTED_OC_SERVER to the protected cluster API server}"
export KUBECONFIG="$PRICETAG_KUBECONFIG"
ACTUAL_OC_SERVER="$(oc whoami --show-server)"
test "$ACTUAL_OC_SERVER" = "$EXPECTED_OC_SERVER"
test "$ACTUAL_OC_SERVER" != "$PROTECTED_OC_SERVER"

if [[ ! "$PREVIOUS_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]]; then
  echo "refusing rollout: saved previous image is not digest-pinned" >&2
  exit 1
fi

oc -n enmaas set image deployment/praxis "praxis=${NEW_IMAGE}"
oc -n enmaas rollout status deployment/praxis --timeout=15m
oc -n enmaas get deployment/praxis
```

Do not restart or scale MaaS, Metering, or Praxis manually. Do not edit Routes
for this path. If a new pod is not Ready, the rollout must stop with old pods
still serving. Investigate or roll back; do not force-delete old pods.

This protects availability while pods are replaced. It does not protect against
a behavior regression in a Ready pod. Use the validation gates in Section 7
before considering the upgrade successful.

## 6. Apply a configuration change

For a configuration change, use the guarded `deploy.sh` path with a reviewed
rendered diff and the approved immutable Praxis digest. The EnMaaS deployment
adds a checksum annotation so a changed `praxis-config` rolls Praxis through
the same `0/1` strategy.

Before applying, verify:

```bash
export PROFILE=enmaas
export NAMESPACE=enmaas
export UPDATE_CONFIG=true
export BUILD_PRAXIS_IMAGE=false
export VERTEX_IMAGE_DIGEST="$PRAXIS_IMAGE_DIGEST"
export CONFIRM_DEPLOYMENT=true

./deploy/openshift/deploy.sh
```

Supply all other required guarded deployment inputs from the approved operator
file. Review `oc diff` before the apply. Stop if the diff contains unrelated
MaaS, Metering, Route, Secret, or NetworkPolicy changes.

## 7. Validate after the rollout

Check all workloads and the public edge:

```bash
oc -n enmaas get deployment maas-api metering-service praxis
oc -n enmaas get pods -l app=praxis -o wide
oc -n enmaas logs -l app=praxis --since=10m \
  | grep -Ei 'error|fatal|panic|config|migration' || true

PATH=/opt/homebrew/opt/coreutils/libexec/gnubin:$PATH \
  ./tools/functional-test.sh --target enmaas --level smoke
```

Run the authenticated tier only with an approved valid test key. Run inference
or full tests only after explicit cost approval. For a behavior change, test
the affected native Anthropic, OpenAI, streaming, tool, and metering paths.

Confirm that:

- all Praxis replicas are Ready and Available;
- no pod restarted unexpectedly;
- Routes remain admitted;
- `/health` and `/ready` pass;
- unauthenticated requests still return `401`;
- inference and metering behavior matches the approved acceptance criteria.

For long-lived streaming traffic, also confirm that an old pod can stop without
cutting an active stream unexpectedly. A successful Deployment rollout alone
is not evidence of graceful stream draining.

## 8. Rollback

For an image-only regression, restore the recorded image. This does not touch
Routes or other workloads:

```bash
export PREVIOUS_IMAGE="$(cat praxis-previous-image.txt)"
if [[ ! "$PREVIOUS_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]]; then
  echo "refusing rollback: saved image is not digest-pinned" >&2
  exit 1
fi
: "${PRICETAG_KUBECONFIG:?Set PRICETAG_KUBECONFIG to the dedicated EnMaaS kubeconfig}"
: "${EXPECTED_OC_SERVER:?Set EXPECTED_OC_SERVER to the EnMaaS API server}"
: "${PROTECTED_OC_SERVER:?Set PROTECTED_OC_SERVER to the protected cluster API server}"
export KUBECONFIG="$PRICETAG_KUBECONFIG"
ACTUAL_OC_SERVER="$(oc whoami --show-server)"
test "$ACTUAL_OC_SERVER" = "$EXPECTED_OC_SERVER"
test "$ACTUAL_OC_SERVER" != "$PROTECTED_OC_SERVER"
oc -n enmaas set image deployment/praxis "praxis=${PREVIOUS_IMAGE}"
oc -n enmaas rollout status deployment/praxis --timeout=15m
```

If the configuration changed, restore `praxis-config-before.yaml` through the
reviewed deployment path, then wait for the checksum-triggered rollout. An
image rollback without a ConfigMap rollback does not restore the old pipeline.

Record the source SHA, image digest, previous image, ConfigMap change, rollout
time, validation results, warnings, and rollback decision. No Praxis upgrade is
complete until the evidence is saved.
