#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# prove-welcome-clients.sh — run every client configuration from the
# PriceTag welcome page (Claude Code, Codex pricetag+qwen, OpenCode) against
# a gateway, headless, and prove each one produced a metering row.
#
# Snippets are the welcome page's own text with {{UNIFIED_URL}} /
# {{OPENAI_URL}} substituted — if the page and the gateway disagree, this
# script finds it, which is exactly what curl-only smoke tests miss.
#
# Usage:
#   ./prove-welcome-clients.sh                      # shadow routes (default)
#   TARGET=prod ./prove-welcome-clients.sh          # prod routes (READ + 1 tiny request each)
#   KEY_FILE=~/.mykey TARGET=shadow ./prove-welcome-clients.sh
#
# Secrets: key is read from $KEY_FILE (or PRICETAG_KEY env) into process env
# only — never argv, never printed. Isolated CLAUDE_CONFIG_DIR / CODEX_HOME /
# OPENCODE_CONFIG so nobody's real client config is touched.
set -euo pipefail

NAMESPACE="${NAMESPACE:-ai-gateway-dogfood}"
TARGET="${TARGET:-shadow}"                    # shadow | prod
KEY_FILE="${KEY_FILE:-$HOME/.mykey}"
SUFFIX=$([[ "$TARGET" == shadow ]] && echo "-shadow" || echo "")
PROMPT="Reply with exactly one word: onboarded"
OUT="${CLAUDE_JOB_DIR:-/tmp}/tmp/welcome-prove"
rm -rf "$OUT"; mkdir -p "$OUT"; chmod 700 "$OUT"

CLIENTS="${CLIENTS:-claude codex opencode hermes}"   # subset filter for reruns
want() { [[ " $CLIENTS " == *" $1 "* ]]; }

log() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
pass() { printf '  PASS %s\n' "$*"; RESULTS+="PASS|$1"$'\n'; }
fail() { printf '  FAIL %s\n' "$*"; RESULTS+="FAIL|$1"$'\n'; }
skip() { printf '  SKIP %s\n' "$*"; RESULTS+="SKIP|$1"$'\n'; }

[[ "$TARGET" == shadow || "$TARGET" == prod ]] || { echo "TARGET must be shadow|prod"; exit 1; }

# ── endpoints ──────────────────────────────────────────────────
host() { oc -n "$NAMESPACE" get "route/$1" -o jsonpath='{.spec.host}' | sed 's|^|https://|'; }
UNIFIED="$(host "ai-gateway-unified$SUFFIX")"
OPENAI="$(host "ai-gateway-openai$SUFFIX")/v1"
KEY="${PRICETAG_KEY:-$(tr -d '[:space:]' < "$KEY_FILE")}"
[[ ${#KEY} -gt 10 ]] || { echo "no key (set PRICETAG_KEY or KEY_FILE)"; exit 1; }
echo "target=$TARGET unified=$UNIFIED openai=$OPENAI (key held in env, ${#KEY} chars)"

db_rows() {   # count + token sums from the (shadow) metering db; prod db probe is manual
    [[ "$TARGET" == shadow ]] || { echo "n/a"; return; }
    # \$DATABASE_URL is escaped ON PURPOSE: it must survive to the pod-side
    # shell (the secret's envFrom), not expand in this script (set -u).
    oc -n "$NAMESPACE" run welcome-probe --rm -i --restart=Never --image=postgres:16-alpine \
        --overrides="{\"spec\":{\"containers\":[{\"name\":\"welcome-probe\",\"image\":\"postgres:16-alpine\",\"stdin\":true,\"securityContext\":{\"allowPrivilegeEscalation\":false,\"capabilities\":{\"drop\":[\"ALL\"]},\"seccompProfile\":{\"type\":\"RuntimeDefault\"}},\"envFrom\":[{\"secretRef\":{\"name\":\"metering-shadow-db-url\"}}],\"command\":[\"sh\",\"-c\",\"psql \\\"\$DATABASE_URL\\\" -tAc \\\"select count(*) from usage_events\\\"\"]}]}}" 2>/dev/null \
        | grep -E '^[[:space:]]*[0-9]+$' | tr -d '[:space:]' || true
}

# ── 1. Claude Code (welcome: env block + modelPicker/modelSettings) ──
log "Claude Code (gateway discovery + hosted Qwen)"
if want claude && command -v claude >/dev/null; then
    export CLAUDE_CONFIG_DIR="$OUT/claude"; mkdir -p "$CLAUDE_CONFIG_DIR"
    python3 - "$CLAUDE_CONFIG_DIR/settings.json" <<'PY'
import json, sys
json.dump({
  "modelPicker": {"options": [{
     "model": "Inferact/Qwen3.8-Flash-Next-NVFP4",
     "label": "Qwen 3.8 Flash Next",
     "description": "Hosted vLLM via PriceTag"}]},
  "modelSettings": {"Inferact/Qwen3.8-Flash-Next-NVFP4": {"effortLevel": "medium"}},
}, open(sys.argv[1], "w"))
PY
    before="$(db_rows)"
    out="$(ANTHROPIC_BASE_URL="$UNIFIED" ANTHROPIC_API_KEY="$KEY" \
        CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1 \
        timeout 180 claude -p "$PROMPT" --model Inferact/Qwen3.8-Flash-Next-NVFP4 \
        --output-format text 2>&1 | tee "$OUT/claude.log" || true)"
    grep -qi onboarded "$OUT/claude.log" && pass "claude-code (reply: $(head -c 40 "$OUT/claude.log"))" \
        || fail "claude-code — see $OUT/claude.log: $(head -c 120 "$OUT/claude.log" | tr '\n' ' ')"
    after="$(db_rows)"; echo "  usage_events rows: $before -> $after"
else
    skip "claude (excluded via CLIENTS or not installed)"
fi

# ── 2. Codex (welcome: pricetag profile + qwen profile) ────────
if want codex && command -v codex >/dev/null; then
    export CODEX_HOME="$OUT/codex"; mkdir -p "$CODEX_HOME"
    # WELCOME-PAGE FINDING: codex >=0.15x rejects the welcome page's legacy
    # [profiles.x] table when --profile is used ("move those settings into
    # <profile>.config.toml"). Providers stay in config.toml; each profile
    # becomes its own file. The welcome page snippet is stale for these
    # versions — flagged to Yos/Noy rather than papered over.
    cat > "$CODEX_HOME/config.toml" <<EOF
[model_providers.pricetag]
name     = "PriceTag"
base_url = "$OPENAI/"
env_key  = "PRICETAG_KEY"
wire_api = "responses"
# /v1 added vs the welcome page (welcome.html line ~422): codex sends
# <base_url>/responses, and the gateway serves /v1/responses — the page's
# snippet 404s as written. Flagged to Yos/Noy as a page fix.
[model_providers.qwen]
name         = "PriceTag hosted"
base_url     = "$UNIFIED/v1"
wire_api     = "responses"
http_headers = { "x-api-key" = "$KEY" }
EOF
    cat > "$CODEX_HOME/pricetag.config.toml" <<EOF
model          = "gpt-5.3-codex"
model_provider = "pricetag"
EOF
    cat > "$CODEX_HOME/qwen.config.toml" <<EOF
model          = "Inferact/Qwen3.8-Flash-Next-NVFP4"
model_provider = "qwen"
EOF
    chmod 600 "$CODEX_HOME"/*.config.toml "$CODEX_HOME/config.toml"
    for prof in qwen pricetag; do
        log "Codex --profile $prof"
        before="$(db_rows)"
        PRICETAG_KEY="$KEY" timeout 180 codex --profile "$prof" exec \
            --skip-git-repo-check "$PROMPT" > "$OUT/codex-$prof.log" 2>&1 || true
        # codex exec ECHOES the prompt, so matching the word alone false-passes;
        # success signal is the final "tokens used" block after the reply.
        grep -q 'tokens used' "$OUT/codex-$prof.log" \
            && grep -qi onboarded "$OUT/codex-$prof.log" && pass "codex/$prof" \
            || fail "codex/$prof — see $OUT/codex-$prof.log: $(tail -c 150 "$OUT/codex-$prof.log" | tr '\n' ' ')"
        after="$(db_rows)"; echo "  usage_events rows: $before -> $after"
    done
else
    skip "codex (excluded via CLIENTS or not installed)"
fi

# ── 3. OpenCode (welcome: anthropic + openai + pricetag-hosted) ─
log "OpenCode"
if want opencode && command -v opencode >/dev/null; then
    python3 - "$OUT/opencode.json" <<PY
import json, sys
json.dump({
  "\$schema": "https://opencode.ai/config.json",
  "provider": {
    "anthropic": {"options": {"baseURL": "$UNIFIED/v1", "apiKey": "{env:PRICETAG_KEY}"}},
    "openai":    {"options": {"baseURL": "$OPENAI",     "apiKey": "{env:PRICETAG_KEY}"}},
    "pricetag-hosted": {
      "npm": "@ai-sdk/anthropic", "name": "PriceTag (hosted)",
      "options": {"baseURL": "$UNIFIED/v1", "apiKey": "{env:PRICETAG_KEY}"},
      "models": {"Inferact/Qwen3.8-Flash-Next-NVFP4": {
        "name": "Qwen 3.8 Flash Next (hosted)",
        "limit": {"context": 262144, "output": 8192}}}}},
  "model": "pricetag-hosted/Inferact/Qwen3.8-Flash-Next-NVFP4"}, open(sys.argv[1], "w"))
PY
    before="$(db_rows)"
    PRICETAG_KEY="$KEY" OPENCODE_CONFIG="$OUT/opencode.json" \
        timeout 180 opencode run "$PROMPT" > "$OUT/opencode.log" 2>&1 || true
    grep -qi onboarded "$OUT/opencode.log" && pass "opencode" \
        || fail "opencode — see $OUT/opencode.log: $(tail -c 150 "$OUT/opencode.log" | tr '\n' ' ')"
    after="$(db_rows)"; echo "  usage_events rows: $before -> $after"
else
    skip "opencode (excluded via CLIENTS or not installed — npm i -g opencode-ai)"
fi

# ── 4. Hermes CLI (welcome §06) — WORKING configs, not the page's ─
# The welcome page's hermes section does not work as written (proved
# 2026-09-16 against shadow, rows aigateway_shadow 23–40):
#  * anthropic provider ignores yaml model.base_url (registry reads
#    ANTHROPIC_BASE_URL env only) → hermes silently used fallback providers
#    and answered with ZERO gateway calls (unmetered!). Fix: BASE_URL lines
#    in ~/.hermes/.env (verified: hermes loads .env into its credential
#    pool — row 33 metered with .env-only).
#  * custom provider ignores env keys entirely, sends literal "no-key"
#    placeholder → needs model.api_key in config.yaml.
#  * default gpt-5.3-codex is responses-only on this gateway; hermes custom
#    speaks chat/completions → 404. gpt-5.4 works; gpt-5.6-luna 400s on
#    hermes' tools+reasoning_effort combo.
# This leg encodes the FIXED recipe. Auxiliary/probe calls land as extra
# model='unknown' 0-token rows — metering records them, dashboards filter.
if want hermes; then
    HERMES_BIN="$(command -v hermes || true)"
    [[ -z "$HERMES_BIN" && -x "$HOME/.local/bin/hermes" ]] && HERMES_BIN="$HOME/.local/bin/hermes"
    if [[ -n "$HERMES_BIN" ]]; then
        export HERMES_HOME="$OUT/hermes"; mkdir -p "$HERMES_HOME"
        umask 077
        cat > "$HERMES_HOME/.env" <<EOF
OPENAI_API_KEY=$KEY
OPENAI_BASE_URL=$OPENAI
ANTHROPIC_API_KEY=$KEY
ANTHROPIC_BASE_URL=$UNIFIED/v1
EOF
        for dialect in qwen gpt; do
            log "Hermes ($dialect dialect)"
            if [[ "$dialect" == qwen ]]; then
                cat > "$HERMES_HOME/config.yaml" <<EOF
model:
  provider: "anthropic"
  default:  "Inferact/Qwen3.8-Flash-Next-NVFP4"
EOF
            else
                cat > "$HERMES_HOME/config.yaml" <<EOF
model:
  provider: "custom"
  default:  "gpt-5.4"
  base_url: "$OPENAI"
  api_key:  "$KEY"
EOF
            fi
            before="$(db_rows)"
            timeout 180 "$HERMES_BIN" -z "$PROMPT" > "$OUT/hermes-$dialect.log" 2>&1 || true
            grep -qi onboarded "$OUT/hermes-$dialect.log" && pass "hermes/$dialect" \
                || fail "hermes/$dialect — see $OUT/hermes-$dialect.log: $(tail -c 150 "$OUT/hermes-$dialect.log" | tr '\n' ' ')"
            after="$(db_rows)"; echo "  usage_events rows: $before -> $after"
        done
    else
        skip "hermes not installed (pip install --user hermes-agent)"
    fi
fi

# ── summary ───────────────────────────────────────────────────
log "Summary (TARGET=$TARGET)"
printf '%s' "$RESULTS" | while IFS='|' read -r verdict name; do
    [[ -n "$verdict" ]] && printf '  %-4s %s\n' "$verdict" "$name"
done
echo
echo "Now check rows + token counts (bridge zero-token finding applies to unified/chat paths):"
echo "  oc -n $NAMESPACE run welcome-probe --rm -i --restart=Never --image=postgres:16-alpine \\"
echo "    --overrides='{\"spec\":{\"containers\":[{\"name\":\"welcome-probe\",\"image\":\"postgres:16-alpine\",\"stdin\":true,\"envFrom\":[{\"secretRef\":{\"name\":\"metering-shadow-db-url\"}}],\"command\":[\"sh\",\"-c\",\"psql \\\"\$DATABASE_URL\\\" -c \\\"select id,model,prompt_tokens,completion_tokens from usage_events order by id desc limit 6\\\"\"]}]}}'"
