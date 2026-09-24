# Praxis upgrade runbook — 2026-09 (dogfood → upstream core 0.5.5)

Branch: `upgrade/2026-09` in `yossiovadia/ai`. Prod: `praxis` deployment
in `ai-gateway-dogfood`, served by 4 routes. Prod stays untouched until
the canary section. Rollback after every step is named explicitly.

> **Provenance map (2026-09-18).** This doc (plus status, port-audit,
> playbook) was purged from the public fork's branch history the day the
> upgrade completed — the branch was rewritten code-identical (22
> commits, base `3bf887c5`, new tip `737ae975`). The adopted build's
> source commit `8305f56e` — the sha behind imagestream tag
> `praxis-ai:adopted-8305f56e` — is `737ae975` on the rewritten branch
> (its tip). Build identity is the image digest
> (`sha256:a693e162a496…`, tagged `adopted-8305f56e` + pinned in
> `deploy/praxis`), unaffected by the rewrite.

## Ground rules

- Prod imagestream `praxis-ai:latest` is NEVER rebuilt from this branch —
  shadow builds go to `praxis-ai-shadow`.
- Shadow stack is rendered from prod manifests by `deploy/openshift/shadow.sh`
  — never hand-copy YAML.
- No `cargo fmt --all` on stable toolchain (repo `rustfmt.toml` is nightly;
  stable reformats ~15 upstream files).

## 1. Local gates (done — evidence in git log)

- [x] 5 filters ported, per-feature commits, each compiles
- [x] `cargo test -p praxis-ai-filters` — 1406 pass (incl. 8 ported
      provider-auto tests); 2 known-failing upstream
      `routing::credential_inject` watcher tests confirmed failing on pristine
      `origin/main` (macOS env)
- [x] `cargo check --workspace --all-targets`
- [x] `praxis-ai --validate` against the live config (see §2) — **passes
      unmodified on core 0.5.5**, exit 0 with dummy provider keys (real keys
      come from the `provider-credentials` secret); all five ported filters
      present in `--dump` output
- [x] `make lint` — all gates pass (clippy ×2 feature sets, nightly
      `fmt --check`, machete, lint-deps/separators, example tests, README
      syncs, inference/responses checks) EXCEPT `lint-filter-docs`, which
      flags 4 upstream docs (`a2a`, `mcp`, `anthropic_messages_format`,
      `openai_responses_format`) from LOCAL rustdoc version drift: our branch
      changes none of their inputs (proven `git diff`-clean against the
      adopted upstream tip), so it fails identically on pristine upstream on
      this machine. Do NOT commit the regenerated churn for those 4.
- [ ] `cargo xtask openai-conformance` — needs `oasdiff` (not installed
      locally); the two new upstream commits since our base are responses-API
      internals we don't route through, so we inherit upstream CI's green.
- [x] rebased onto `origin/main` tip (`3bf887c5`, 2026-09-16) — clean replay
      of all 11 commits, full local gate re-run: filters tests, example tests
      (incl. new `stream-usage-inject` suite test), `--validate` exit 0 on
      the rebuilt binary.

## 2. Config migration

The LIVE cluster `praxis-config` cm — not the committed manifest — is the
source of truth for prod behavior, and it had drifted from both the
committed dogfood manifest and the branch's worktree. Extract + validate
against the new binary:

```bash
oc -n ai-gateway-dogfood get cm praxis-config -o jsonpath='{.data.praxis\.yaml}' > /tmp/praxis-cfg.yaml
cargo run -p praxis-ai-proxy -- --config /tmp/praxis-cfg.yaml --validate
```

Result (2026-09-16): the live config **failed at first** with
`token_count: unknown variant 'auto'`. The cm had been hot-reload-patched
(praxis serves the old config on reload failure — no crash) with Noy's
`provider: auto` per-path dialect selection, which exists only on his
branch `noyitz/fix/token-count-provider-auto` and in the running untagged
prod image. Two changes landed on this branch:

- `5df19c4f` — cherry-pick of Noy's `8b73b4db`, resolved against upstream
  0.5.5's `token_count` (kept upstream's `max_scratch_bytes` machinery);
  all 8 of his provider-auto tests pass.
- `e760daf7` — `deploy/openshift/praxis.yaml` rebuilt from the
  live-extracted config (literal-block style preserved, byte-exact
  roundtrip asserted) plus one required migration:
  `max_scratch_bytes: 1048576` under `token_count`. Upstream replaced the
  old binary's hardcoded 1 MiB SSE scratch with a configurable 64 KiB
  default; Responses-API SSE events exceed 64 KiB, so the default would
  silently zero Qwen-via-Codex token metering.

- SSRF-guard migration (caught by shadow proving, 2026-09-16): core 0.5.5
  gates filter sub-requests to non-public IPs behind an opt-in
  (`apis/src/callout_target.rs`). Real streaming request through
  `ai-gateway-unified-shadow` streamed fine (Responses SSE
  `response.completed` with usage) but wrote **zero** rows —
  `external_metering` logs `target resolved to blocked non-public
  address …; set the filter's private-target opt-in to true`. Fix:
  `allow_private_endpoint: true` on all four `external_metering` entries
  (commit `3fa269e7`); A/B against prod proved the OLD binary reports
  fine (no guard yet), so prod needed no urgent change — but ADOPTION
  does. `api_key_auth`'s maas-api validate call is not guarded (verified
  working unchanged). Post-fix the shadow wrote the expected row:
  prompt 62 / completion 24 / total 86, username+group carried end-to-end.

- Reasoning-effort compatibility (new filter, 2026-09-16, requested after
  shadow proving): Qwen's vLLM chat template rejects `reasoning_effort`
  outside `xhigh`/`medium`/`low`; the `high` default that OpenAI-style
  clients send (and the Responses bridge's verbatim copy of
  `reasoning.effort`) 400s with `BadRequestError` (shadow rows 56–57,
  same seen upstream-side on the emerg host). New `reasoning_effort_map`
  filter (unified chain, after `content_normalize`) rewrites both the
  chat-completions top-level `reasoning_effort` and the Responses
  `reasoning.effort` through a configurable map (defaults `high→xhigh`,
  `minimal→low`). Scoped to an explicit `models` list — mandatory and
  non-empty at config parse — matched exactly against the body `model`
  field, so `gpt-*` requests, where `high` is legitimate, are never
  rewritten. Model gating, not cluster gating: the unified chain's
  `model_to_header` puts the pipeline in pre-read mode, where body
  hooks run *before any filter's header phase*, so the router's
  cluster does not exist yet when a body rewrite must be committed
  (proved in finding 7 of the status doc; first cluster-gated cut
  silently rewrote nothing). New qwen models on the qwen-flash route
  must be added to this list — forgetting fails loudly (the backend
  400s). **Adopt: the prod cm must carry this filter entry alongside
  the two migrations above.**

`--validate` now exits 0. **Open coordination item:** the running prod
image (`sha256:ae83fb76…`) has no git provenance (no `git-<sha>`
imagestream tag, no change-cause annotation), and Noy has three further
open branches (`feat/overlay-apikey-strategy`,
`feat/token-count-prompt-cache`, fork `fix/token-count-responses-api`).
Before canary/adopt, confirm none of those are in the running image — the
adopted branch must be a superset of what prod actually runs.

## 3. Shadow stack

```bash
./deploy/openshift/shadow.sh up        # db + secret + bc/is + deploy + cm + svc + *-shadow routes
./deploy/openshift/shadow.sh release   # builds THIS branch into praxis-ai-shadow, rolls shadow only
./deploy/openshift/shadow.sh status
```

Isolation: shadow selectors are `app=praxis-shadow`; shadow metering writes
`aigateway_shadow` db (never prod `usage_events`); shadow routes are
`ai-gateway-*-shadow`. Rollback of the whole shadow: `shadow.sh teardown`.

## 4. Prove

1. llm-katan replay (zero provider spend): point test traffic at shadow routes.
2. `SMOKE_API_KEY=sk-... curl https://<unified-shadow-host>/v1/models` + real
   chat request incl. streaming (stream_usage_inject must produce non-zero
   token counts in `metering-service-shadow`'s `usage_events`).
   **DONE 2026-09-16:** 401s/200s correct per listener dialect
   (`x-api-key` on anthropic/unified, `authorization` on openai — the live
   config's `token_header` choice, not a bug); `/v1/responses` stream on
   unified → non-zero row in `aigateway_shadow.usage_events` (see §2 SSRF
   item). Note: chat-completions→Responses *bridge* path (qwen on the
   anthropic router) emits no client usage chunk on EITHER binary —
   prod-parity, flagged to Noy as pre-existing, not an upgrade regression.
3. Before/after repros for each ported fix (stream usage injection,
   content_normalize on vLLM, model_catalog envelopes, model_access 403s).
4. Yos + Noy daily-drive `ai-gateway-*-shadow` for a day.
5. Compare shadow vs prod `usage_events` rows for identical requests.
6. Welcome-page client matrix: `scripts/prove-welcome-clients.sh`
   (TARGET=shadow|prod, CLIENTS=claude codex opencode). **DONE 2026-09-16:**
   4/4 pass against shadow, all success rows metered non-zero; found two
   welcome-page snippet bugs (status doc finding 5).

## 5. Canary (the only step that touches prod traffic)

OpenShift route weighted backends. Weights are RELATIVE per route and the
primary does NOT shrink automatically — set both sides explicitly.
Yos's plan: straight 50/50 (all four routes), Yos + Noy testing live.

```bash
# GO (all four routes, one command each):
for r in ai-gateway-unified ai-gateway-anthropic ai-gateway-openai ai-gateway-benchmark; do
  oc -n ai-gateway-dogfood set route-backends $r praxis=50 praxis-shadow=50
done

# verify spec (route-backends is NOT a gettable resource — jsonpath):
oc -n ai-gateway-dogfood get route ai-gateway-unified \
  -o jsonpath='{.spec.to.name}={.spec.to.weight} ALT={.spec.alternateBackends}{"\n"}'

# verify empirically — MUST be a FULL-CHAIN request (tiny chat completion);
# /v1/models short-circuits in model_catalog BEFORE the marker filter and
# 401s short-circuit in auth — both show "old" even from the new build:
curl -sD - -o /dev/null https://<prod-unified-host>/v1/chat/completions \
  -H "x-api-key: $KEY" -H content-type:application/json \
  -d '{"model":"Inferact/Qwen3.8-Flash-Next-NVFP4","messages":[{"role":"user","content":"hi"}],"max_tokens":4}' \
  | grep -i x-gateway-build     # present => this response came from the new build

# router truth (if split looks wrong): both router pods' runtime state —
# servers all state=2 weight=256 means the router IS splitting:
oc -n openshift-ingress exec deploy/router-default -- sh -c \
  "echo 'show servers state' | socat /var/lib/haproxy/run/haproxy.sock -" \
  | grep 'ai-gateway-unified '
```

**FLIPPED 2026-09-17 03:5x** — all four routes `praxis=50 praxis-shadow=50`
(spec verified per route), empirical split 6 new / 6 old on 12 real chat
probes to the prod unified host, shadow pods 0 errors, prod metering
ingesting real users' rows during the window.

**FULL FLIP 2026-09-17 23:4x UTC** — all four routes `praxis=0
praxis-shadow=100` (spec verified per route); 6/6 real chat probes to the
prod unified host returned 200 + `x-gateway-build: new`. Gates at flip
time: error parity across stacks (only failure class on BOTH was
long-stream resets to the emerg-Qwen host, 1–2 per 8h), shadow memory
91–102Mi of 256Mi, zero restarts, Yos's per-source row check. Post-flip
first 4 min under 100%: zero shadow errors; CPU doubled as expected
(2–3m → 4–8m of 1000m). Old stack stays warm and unexposed — deployment
untouched (house rule), rollback is weight-only, seconds. **Soak clock
starts now**; §6 adopt after the agreed soak period.

Pre-flip gates (all must hold, same day): battery green on shadow,
shadow cm rendered with `SHADOW_METERING_MODE=prod` applied (rows →
prod metering with `source: praxis-ai-shadow*`), live `praxis-config`
re-diffed against the branch mirror (hand-patch hazard), prod pods
2/2 @ `ae83fb…`.

Watch error rate + latency + prod `usage_events` rows tagged
`praxis-ai-shadow` between/after steps. Responses from the new build
carry header `x-gateway-build: new` on ALL routes — Yos/Noy keep their
unchanged URLs and attribute per request.

**Rollback (any time, seconds, no pod churn), per route:**

```bash
for r in ai-gateway-unified ai-gateway-anthropic ai-gateway-openai ai-gateway-benchmark; do
  oc -n ai-gateway-dogfood set route-backends $r praxis=100 praxis-shadow=0
done
# clean removal afterwards: patch alternateBackends: []
```

Caveat: rows already written with `source: praxis-ai-shadow` are REAL
spend and stay in prod (correct bookkeeping, not pollution); dashboards
can filter by source during the canary.

## 6. Adopt

At 100% shadow for a soak period:
1. `release.sh` prod: point the ORIGINAL `praxis` deployment/BC back at the
   adopted branch (or promote shadow resources by re-rendering prod from
   validated praxis.yaml).
2. Keep old pods unexposed a week (same insurance pattern as postgresql-0).
3. `shadow.sh teardown`, re-point watchers/dashboards.

**ADOPTED 2026-09-18 00:4x UTC — via image PROMOTION, not a release.sh
rebuild** (standing rule honored: `praxis-ai:latest` was never rebuilt from
the upgrade branch; the promoted image is the bit-for-bit build that had
served 100% of traffic error-clean for ~1.5h + full parity overnight):

- `oc tag praxis-ai-shadow@sha256:a693e162…` → `praxis-ai:adopted-8305f56e`
  (`--reference-policy=local`, survives a future shadow teardown + GC);
  rollback anchor `praxis-ai:legacy-ae83fb` tags the pre-upgrade prod image.
- Pre-check: live shadow config (carrying all traffic) diffed vs the branch
  manifest — deltas were ONLY the intentional canary-isms (source tag
  `praxis-ai-shadow*`, `x-gateway-build` marker); zero hand-patch drift.
- `praxis-config` = branch bytes (live cm backed up to
  `/tmp/praxis-config-backup.yaml`); `oc set image deploy/praxis` → proven
  digest; rolled at route weight 0, `maxSurge=1 maxUnavailable=0`, readiness
  gate — users never pointed at those pods during the roll.
- Verified IN-CLUSTER before flipping: full-chain Qwen chat `200` 54/8
  tokens; `reasoning_effort: high` fingerprint `200` (old binary 400s —
  behavioral proof of build identity, no marker needed).
- Flipped all four routes `praxis=100 praxis-shadow=0`; public probes 6/6
  `200`, marker header absent (correct — prod config). Post-flip: zero
  restarts, one known long-stream reset (the week's ambient 1–2/8h class).
- From the flip forward, ledger rows are stamped `source: praxis-ai` again;
  `praxis-ai-shadow*` rows stop accruing (historical ones can be normalized
  by a one-time UPDATE — cosmetic, Yos's call).

**Loose ends (non-urgent):**
1. `praxis` has NO BuildConfig — the next code release must run
   `release.sh` from the adopted branch once (post-adopt that's the normal
   source of truth, rule retired) to restore the build pipeline.
2. Keep praxis-shadow deployment + both imagestream tags a week
   (postgresql-0 insurance pattern), then `shadow.sh teardown`.
   **UPDATED 2026-09-18:** shadow deployments SCALED TO 0 after a
   triple-check found zero traffic on any layer (router weight 0 + no
   pools + 0 log lines/2h + 0 ledger rows since flip) — idle pods
   confused other Claude sessions. Stack itself (routes, shadow db,
   secret, BC/IS) stays until ~Oct 2 for month-end metering insurance.
   Wake: `oc scale deploy/praxis-shadow deploy/metering-service-shadow
   --replicas=2` (pods ~30s) or `shadow.sh up` (re-applies manifests).
3. Dashboard SQL check (Yos): zero NEW `source LIKE 'praxis-ai-shadow%'`
   rows after 2026-09-18 00:4x UTC.

## Known risks

- `credential_inject` watcher tests fail on macOS — upstream, pre-existing.
- Route weight = request-level split; long-lived streams stay on their
  original backend (fine).
- api_key_auth/model_access open question (custom filters vs upstream auth
  machinery) does NOT gate the upgrade — re-expressing them is a follow-up.

## Capacity baseline — ~30 active users (2026-09-17 19:0x UTC, 50/50 canary live)

`oc adm top pods` samples; limits in parentheses. Zero restarts / OOMKills
across the namespace.

| pod | CPU | memory |
|---|---|---|
| praxis ×2 (1000m / 256Mi) | 3–5m | 84–98Mi |
| praxis-shadow ×2 (1000m / 256Mi) | 6–11m | 64–107Mi |
| metering-service (200m / 128Mi) | 1–8m | 15–26Mi |
| maas-api (200m / 128Mi) | 1m | 17Mi |
| llm-katan ×2 | 2m | 35–44Mi |
| aigateway-pg-1 primary (1000m / 1Gi) | 203m (758m spike) | 294Mi |
| aigateway-pg-2/3 replicas | 7–8m | 138–192Mi |
| nodes ×3 | 6–16% | 39–54% |

Reading: proxies carry ~100× CPU headroom; worst-case stream capture
(1 MiB body + 1 MiB scratch each) is noise at this scale. First component
to hit a wall as users grow is **pg-1** — only pod near its limit, while
replicas serve no reads. Watch items before ~100 users: point dashboard
reads at pg-2/pg-3; raise pg-1 CPU limit; if praxis memory crosses
~200Mi, raise the 256Mi limit rather than shrinking `max_scratch_bytes`
(metering correctness beats memory thrift).
