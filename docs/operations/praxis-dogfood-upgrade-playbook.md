# Praxis dogfood upgrade playbook

Generalized from `upgrade-2026-09-runbook.md` (core 0.5.5 upgrade, shipped
2026-09-18 — that runbook is the worked example). Follow this on the NEXT
upgrade (core or filters). Timescales: ~2 days local+shadow, then soak/canary
at your own pace; every phase names its rollback.

## Invariants (never broken, any phase)

1. Prod `praxis` deployment is the team-wide gateway: never scale to zero,
   never starve, touch it only via image/config + route weights.
2. Never rebuild `praxis-ai:latest` from an unproven branch. Unproven code
   goes to `praxis-ai-shadow`; adoption PROMOTES a proven digest (below).
3. Shadow stack comes from `deploy/openshift/shadow.sh` only — never
   hand-copy YAML. Prod manifests are rendered, not forked.
4. Config truth is the LIVE configmap, not the committed manifest. Diff
   live vs the branch mirror (`deploy/openshift/praxis.yaml`) before every
   traffic move — hot-reload hand-patches are the classic hazard (praxis
   serves the OLD config on reload failure, silently).
5. The cm mounts via subPath → config changes need a rollout, no hot reload.
6. Key material flows file→header only; never argv, logs, or git.
7. `cargo fmt --all` on stable is banned (repo rustfmt.toml is nightly).

## Phase 0 — local gates

- Rebase onto upstream tip; replay cleanly; full re-run after rebase.
- `cargo test -p praxis-ai-filters`, `cargo check --workspace --all-targets`,
  `make lint`.
- KNOWN LOCAL NOISE on macOS hosts: `credential_inject` watcher tests and
  4 rustdoc drift docs (a2a/mcp/anthropic_messages_format/
  openai_responses_format). Reproduce on PRISTINE origin/main before
  blaming your branch; do NOT commit regenerated churn.

## Phase 1 — config migration

- Extract the live cm; validate against the NEW binary:
  `oc get cm praxis-config -o jsonpath='{.data.praxis\.yaml}' > /tmp/cfg.yaml`
  then `cargo run -p praxis-ai-proxy -- --config /tmp/cfg.yaml --validate`.
- Watch for upstream default changes that the old binary hardcoded (the
  2026-09 case: 64 KiB SSE scratch default silently zeroed Responses-API
  metering until `max_scratch_bytes: 1048576` was set).
- Rebuild the branch mirror from the live extraction (byte-exact roundtrip
  asserted) + only the required migrations. New filters go in the mirror
  too — the prod cm must carry them AT adopt.

## Phase 2 — shadow stack & proving

- `shadow.sh up` / `release` / `status` / `teardown`. Isolation:
  `app=praxis-shadow` selectors, `*-shadow` routes, separate shadow DB.
- Shadow metering writes the shadow DB; the canary gate flips it to prod
  metering with `source: praxis-ai-shadow*` (see `SHADOW_METERING_MODE`).
- Prove before touching traffic:
  - per-listener auth dialects (401 unauth / 200 authed, header per config);
  - real STREAMING chat + Responses requests metered NON-ZERO in the shadow
    ledger (SSRF guard: `allow_private_endpoint: true` on filters calling
    private IPs — core ≥0.5.5 blocks them by default);
  - welcome-client matrix: `scripts/prove-welcome-clients.sh`
    (claude / codex / opencode / hermes) — every success path metered;
  - before/after repro for each behavior this upgrade changes.

## Phase 3 — canary (first traffic touch)

- Route weighted backends, weights RELATIVE — set BOTH sides explicitly:
  `oc set route-backends $r praxis=50 praxis-shadow=50` (all 4 routes).
- `set route-backends` is not a gettable resource: verify via jsonpath on
  `.spec.to.weight` / `.spec.alternateBackends`.
- Verify EMPIRICALLY with FULL-CHAIN requests only: `/v1/models`
  short-circuits in model_catalog and 401s in auth — both answer "old"
  even from the new build. The shadow renderer injects a response header
  (`x-gateway-build: new`) as the build marker; prod config must NOT have it.
- Router truth if split looks wrong: `show servers state` via socat on
  `deploy/router-default` in openshift-ingress.
- Rollback (seconds, no pod churn): weights back to `praxis=100
  praxis-shadow=0`.

## Phase 4 — 100% new build

- `set route-backends $r praxis=0 praxis-shadow=100` × 4.
- Watch: error-parity between stacks (`Fail to proxy` classes — long-stream
  resets to the emerg-Qwen host are ambient at ~1–2/8h, both stacks),
  shadow memory vs the 256Mi limit (action line ~200Mi), ledger rows with
  `source: praxis-ai-shadow` flowing real users.
- Long-lived streams stay on their original backend across weight changes.

## Phase 5 — adopt (via image PROMOTION — no rebuild)

The image that carried 100% traffic clean is the release artifact:

1. `oc tag praxis-ai-shadow@sha256:<proven> praxis-ai:adopted-<gitsha>
   --reference-policy=local` — into the PROD imagestream so a later
   `shadow.sh teardown`/registry GC can't strand prod.
2. Tag the current prod image `praxis-ai:legacy-<sha>` (rollback anchor).
3. Diff the configmap LIVE-CARRYING-TRAFFIC vs the branch mirror — deltas
   must be ONLY the intentional canary-isms (source tag, marker header).
4. Back up the live cm; apply the branch bytes as `praxis-config`.
5. `oc set image deploy/praxis praxis=…praxis-ai@sha256:<proven>` — rolls
   at weight 0 (`maxSurge=1 maxUnavailable=0`): users never touch these
   pods during the roll. Gate on `rollout status`.
6. Verify IN-CLUSTER through the service before flipping: a full-chain
   chat (non-zero usage) plus a BEHAVIORAL FINGERPRINT of something the
   old binary fails and the new one passes (2026-09: `reasoning_effort:
   high` at Qwen — old 400s, new maps→200). The marker header is gone by
   now; behavior is the identity proof.
7. Flip routes home: `praxis=100 praxis-shadow=0` × 4; public probes 6/6.
8. Paper trail: runbook append, commit, push.

Rollback at any point before step 7: prod never had traffic — just unset.
After step 7: `oc set image deploy/praxis praxis=…praxis-ai:legacy-<sha>`
+ cm backup + weights, all seconds.

## Post-adopt hygiene

- Ledger rows revert to `source: praxis-ai` automatically; the canary-window
  `praxis-ai-shadow*` rows are honest history — normalize via one-time
  UPDATE only if the dashboard's source grouping bothers you.
- Keep the shadow stack + both imagestream tags a week (postgresql-0
  insurance pattern), then `shadow.sh teardown`.
- Rebuild the pipeline: if prod has no BuildConfig (hand-pushed images),
  run `release.sh` from the adopted branch once — post-adopt that IS the
  source of truth and the no-rebuild rule retires.
- Verify via SQL: zero NEW shadow-tagged rows after the flip.

## Env gotchas that cost time in 2026-09 (Claude Code side)

- Auto-mode classifier rides the SONNET tier — remapping tiers to a
  capacity-shared model (the Qwen box) is a self-DoS when that box loads;
  classifier timeout blocks all classifier-gated tools (chicken-and-egg:
  including the fix). Typed `!` commands and read-only tools survive.
- `oc rsh` and password-probes get classifier-blocked; don't retry-loop.
- CNPG `Database` CR signals done via `status.applied=true`, not a Ready
  condition. `oc create` clones strip `status` (prod build numbers leak).
- zsh chokes on bare `echo ===` mid-chain.
