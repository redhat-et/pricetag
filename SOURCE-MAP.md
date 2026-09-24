# Source Map

This repository is a clean public import of PriceTag-owned deployment and
operations material.

## Imported Inputs

- Praxis deployment configuration from the adopted production tree
  `9be8880c` (`dogfood-adopted-9be8880c`).
- Full fresh-environment deployment guide from the rewritten metering service
  source.
- Database design, backup, and restore material from the rewritten metering
  repository.
- `ttft-bench.py` as a sanitized deployment validation tool.
- `praxis-dogfood-upgrade-playbook.md`, the upgrade runbook, and the welcome-client
  proof script from the private operations runbooks.

## Deliberately Excluded

- Praxis, IPP, MaaS, Gateway API, and controller source repositories.
- Partner-owned documentation repositories.
- Headroom and compression work.
- The obsolete single-pod PostgreSQL manifests and dead Qwen proxy relic.
- Legacy sandbox architecture documentation.
- Personal keys, kubeconfigs, provider credentials, live dashboard URLs, and
  personal environment artifacts.

Runtime dependencies are referenced by approved image or source revisions;
their upstream history is not copied here.
