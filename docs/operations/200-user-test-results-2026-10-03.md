# EnMaaS 200-user production test results

This report records the EnMaaS production baseline executed on October 3, 2026.
It separates synchronized request concurrency from a sustained 200-user
workload so the results are not interpreted as a general 200-concurrent-user
capacity claim.

The reusable procedure and safety controls are documented in
[key-concurrency-test.md](key-concurrency-test.md). The test implementation is
`tools/enmaas-key-load-test.py`.

## Environment and client

- Target: `https://api.enmaas.devshift.net`
- Identities: 200 distinct production user IDs and API keys
- Client: one Python worker per selected user
- Connection behavior: a fresh TLS connection for every request; no connection
  pooling or HTTP/2 multiplexing
- Retries: none
- Latency: client-observed time through DNS, OpenShift ingress, Praxis and the
  applicable backend; inference latency includes reading the complete response
- Result artifacts: mode `0600`, with UUIDs and key fingerprints but no API keys
  or email addresses

## Workload

### Authentication

The auth-only workload sent `GET /v1/models`. It exercised ingress, Praxis API
key authentication and MaaS without provider inference or usage events.

### Inference

The inference workload sent one non-streaming, single-turn OpenAI-compatible
Chat Completions request per scheduled user:

```json
{
  "model": "<rits/zai-org/glm-5-3 or gpt-5.4>",
  "messages": [
    {
      "role": "user",
      "content": "Reply with exactly: loadtest-<run-id>-<sequence>"
    }
  ],
  "max_tokens": 64
}
```

For `gpt-5.4`, the request used `max_completion_tokens: 64` instead of
`max_tokens`. The payload omitted `stream`, so streaming was off. It did not use
tools, functions, multiple turns or prior conversation context.

- Input sequence length (ISL): the clean GPT-5.4 run metered 6,200 input tokens
  across 200 requests, or 31 per request. A GLM-specific ISL was not captured in
  the load-test artifact; the exact request text is shown above.
- Output sequence length (OSL): capped at 64 tokens. The clean GPT-5.4 run
  metered 4,800 output tokens, or 24 per request. A GLM-specific average was not
  captured in the load-test artifact.

## Traffic profiles

### Synchronized bursts

The ramp used barriers to release fresh connections together at concurrency
levels `5, 10, 25, 50, 100, 200`. This was a worst-case burst, not a sustained
200-request concurrency level.

### Sustained 200-user traffic

Each of 200 users sent approximately one request per minute for 15 minutes.
Every user received a randomized initial phase and each subsequent interval had
plus or minus 20% jitter. This generated 2,996 attempts at 3.319 requests per
second. Requests were not synchronized and actual in-flight concurrency varied
with response latency.

## Results

### Authentication

| Scenario | Success | p50 | p95 | p99 | Maximum |
|---|---:|---:|---:|---:|---:|
| Final 200-user ramp step | 200/200 | 301 ms | 606 ms | 649 ms | 660 ms |
| Warm authentication cache | 200/200 | 428 ms | 624 ms | 652 ms | 656 ms |
| After 360 seconds, past the 300-second cache TTL | 200/200 | 804 ms | 831 ms | 834 ms | 835 ms |
| 20 rounds of 200 users | 4,000/4,000 | 317 ms | 532 ms | 667 ms | 1,492 ms |

The 20-round soak ran over ten minutes with 30 seconds between rounds. No test
gate failed and no provider requests or usage events were created.

### Synchronized inference burst

| Model | Success | p50 | p95 | p99 | Maximum | Metering |
|---|---:|---:|---:|---:|---:|---:|
| GLM 5.3 | 200/200 | 3.35 s | 4.36 s | 5.03 s | 5.12 s | 200/200 |
| GPT-5.4 clean retest | 200/200 | 1.89 s | 13.21 s | 13.69 s | 13.95 s | 200/200 |

Both providers returned all 200 responses successfully and reconciled exactly
one usage event per expected user on the clean runs. Neither met the then-current
p99 target of less than four seconds for a synchronized 200-request burst. GLM
had the better tail; GPT-5.4 had the better median.

The clean GPT-5.4 run metered 11,000 total tokens and an estimated cost of
`$0.0875`.

### Sustained GLM workload

| Measure | Result |
|---|---:|
| Attempts | 2,996 |
| HTTP 200 | 2,995 |
| HTTP failure | 1 router 502 |
| Request rate | 3.319 requests/second |
| p50 | 1.92 s |
| p95 | 7.13 s |
| p99 | 7.60 s |
| Expected usage events | 2,995 |
| Recorded usage events | 2,843 |
| Missing usage events | 152 (5.1%) across 117 users |

Metering logs contained exactly 2,843 matching `event recorded` entries, placing
the loss before Metering service persistence. Praxis logged usage-report and
balance-check subrequest deadline failures while application workloads and RDS
were not saturated. This accounting failure is tracked as
[pricetag#59](https://github.com/redhat-et/pricetag/issues/59). Sustained provider
testing was paused because successful responses could be unmetered and the same
failure path could bypass dollar-cap checks under fail-open behavior.

## Platform observations

During the recorded burst tests, MaaS, Metering and Praxis remained available,
pods did not restart, monitoring targets remained up and RDS connection usage
stayed near 0.7% with 12 connections. The sustained accounting failure therefore
was not accompanied by observed workload or database saturation.

## Scope and limitations

These results establish an authentication baseline and expose burst latency and
metering behavior for a small synthetic prompt. They do not represent:

- 200 continuously in-flight inference requests
- streaming or time-to-first-token performance
- long-context or high-ISL workloads
- long generated responses
- coding-agent, tool-call or multi-turn workflows
- client connection reuse or HTTP/2 multiplexing
- a successful sustained metering or dollar-cap test

Separate workload profiles are required before extrapolating to those cases.

## Supporting records

- [Harness and result discussion, PR #58](https://github.com/redhat-et/pricetag/pull/58)
- [Sustained accounting failure, issue #59](https://github.com/redhat-et/pricetag/issues/59)
