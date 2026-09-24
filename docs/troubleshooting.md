# PriceTag Troubleshooting

This guide is symptom-first. Run read-only probes first. Do not restart,
scale, or patch the team deployment while diagnosing it.

## Requests Return 401

Check the listener dialect and header:

- Anthropic and unified routes use `x-api-key`.
- OpenAI routes use `Authorization: Bearer ...`.
- Confirm the key exists and is active in MaaS.
- Check `maas-api` logs and the gateway logs for validation failures.

An unauthenticated request should return `401` on every protected route.

## Requests Return 403

Check the model name and the user's MaaS group membership. Inspect the
`model_access` configuration and verify the requested model is present in the
appropriate allowlist.

## Dashboard Has No Usage

Check the complete path:

1. Gateway logs for the `external_metering` response.
2. Metering service logs for CloudEvent ingestion.
3. The `usage_events` table in the primary CNPG service.
4. The model name and provider recorded in the event.
5. `model_pricing` for a matching pricing row.

The usage database is the metering log of record. A successful model response
without a corresponding `usage_events` row is a deployment defect.

## Dashboard Numbers Are Slow or Inconsistent

Inspect `/api/v1/admin/rollups` and the metering service logs. The dashboard
should fall back to raw reads when rollup parity is unhealthy. Do not repair
the rollup by deleting ledger data. Rebuild derived data from `usage_events`
using the documented CNPG operations.

## Database Problems

Check the CNPG cluster and instances:

```bash
oc get cluster aigateway-pg -n "$NS"
oc get pods -n "$NS" -l cnpg.io/cluster=aigateway-pg
oc describe cluster aigateway-pg -n "$NS"
```

The production profile uses three CNPG instances and the RWO storage class
selected for the environment. A primary failure should be handled by CNPG;
do not manually promote a pod.

## Backup or Restore Problems

Check the `ScheduledBackup`, the `cnpg-backup-cos` Secret, the COS endpoint,
the pinned SigV4 region, and the CNPG operator logs. A backup object existing
is not enough; perform a restore drill in a separate namespace and verify the
restored ledger counts.

Never run schema-wipe or repair jobs against the live team namespace without
explicit approval and a verified backup.

## Latency or Streaming Problems

Use `tools/praxis-overhead.py` for paired direct-upstream versus gateway
measurements. Report medians for warm and cold connections rather than a
single request. Check gateway pod logs, OpenShift router logs, and provider
connection resets separately.

For streaming failures, verify the route timeout, edge TLS route, provider
SNI, and the client's HTTP protocol. Do not infer a gateway regression from a
single cold request.

## WebSocket Upgrade Behavior

Probe the exact client upgrade shape with HTTP/1.1 `Upgrade` and
`Connection` headers. The `reject_upgrade` filter is intended to prevent
unmetered opaque tunnels, but a configured filter is not proof that the live
transport detects every client shape. Record the request shape, status, and
whether a usage row was written.

The acceptance test must reproduce the original client transport, not only a
synthetic curl variant.

## Welcome-Client Validation

Run `tools/prove-welcome-clients.sh` against a test or shadow deployment for
the supported Claude Code, Codex, and OpenCode paths. A successful request and
a non-zero metering row are both required.
