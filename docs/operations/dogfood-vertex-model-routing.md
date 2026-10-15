# Dogfood model routing

The `dogfood` PriceTag profile now renders the same shared model-routing
fragments used by EnMaaS. This includes:

- OpenAI-format catalog entries for GPT, Gemini, GLM, and Vertex Claude models.
- Anthropic-format catalog entries for Claude, hosted models, and GLM.
- `intelligent_route` mappings from projected, hot-reloaded routing overlays;
  the legacy model-to-provider and core router filters are removed from the
  dogfood OpenAI and unified chains.
- Candidate `target_model` values rewrite public model IDs to Vertex publisher
  IDs while preserving the client model for response adapters.
- Vertex rawPredict and Gemini Chat Completions routing.
- GCP service-account credentials scoped to the Vertex cluster.
- The shared model-access and metering filters.
- A 10-billion-token monthly dogfood safety budget.

The two overlay files are mounted without `subPath` so ConfigMap symlink
updates are visible to Praxis's watcher:

```text
/etc/praxis/routing/openai/routing-overlay.json
/etc/praxis/routing/unified/routing-overlay.json
```

Changing a candidate's cluster, target model, admission state, or weights can
therefore be tested by updating the corresponding ConfigMap without a Praxis
pod restart. Provider clusters, endpoints, TLS, and static credentials remain
in the rendered Praxis configuration.

The dogfood profile requires an ET Praxis image containing the Vertex filters.
Deploy only from pushed commits:

```text
PROFILE=dogfood
VERTEX_PROJECT=<approved-gcp-project>
VERTEX_SA_KEY_FILE=<path-to-service-account-json>
PRAXIS_SOURCE_SHA=<40-character-pushed-redhat-et-praxis-ai-sha>
METERING_SOURCE_SHA=<40-character-pushed-pricetag-metering-sha>
PRAXIS_AI_FEATURES=full,gcp-adc-filter
```

Run the deployment script's normal guarded preflight before applying changes.
The script creates the `vertex-sa-key` and internal metering Secret mounts,
renders the shared Vertex fragments, pins the Praxis image by digest, and
rolls Praxis when the rendered config checksum changes.

After deployment, verify both catalog envelopes and the serving paths:

```bash
curl -skS "$GATEWAY_URL/v1/models" \
  -H "Authorization: Bearer $KEY"

curl -skS "$GATEWAY_URL/v1/models" \
  -H "x-api-key: $KEY" \
  -H 'anthropic-version: 2023-06-01'
```

Then smoke-test at least one model from each route family: GPT through
OpenAI Chat/Responses, Gemini through Vertex Chat Completions, Claude through
Anthropic Messages, GLM through the hosted Anthropic route, and a Vertex
Claude model through the rawPredict adapter. A catalog entry must not be
considered usable until its corresponding inference request succeeds.
