# Praxis overlay-router blue/green cutover

The first transition from static `router`/`model_to_provider` chains to
`intelligent_route` overlay chains is a breaking Praxis configuration change.
Do not apply it with the normal `deploy.sh` RollingUpdate: old and new pods can
serve the same traffic while interpreting the shared configuration differently.

## Required sequence

1. Build and push the Praxis image from the reviewed ET Praxis commit.
2. Create a parallel green Praxis deployment using the new image and the new
   overlay-backed `praxis-config`; keep the existing blue deployment serving
   the production service.
3. Wait for green readiness and run the complete catalog/auth/inference smoke
   matrix against green, including OpenAI, Gemini/Vertex, Anthropic/Vertex,
   GLM, management paths, and metering fail-closed behavior.
4. Shift traffic from blue to green using the platform's controlled route or
   service cutover. Do not mix blue and green pods behind the same service
   during validation.
5. Observe green under traffic and retain blue for rollback until the agreed
   soak period completes.
6. Only after the cutover, run the normal deploy flow for compatible changes
   and overlay-only hot reloads.

`deploy.sh` refuses to apply a detected static-router → overlay-router
transition in place. This guard is intentional; the blue/green cutover is an
explicit operational gate rather than an implicit rolling update.
