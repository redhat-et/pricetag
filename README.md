# PriceTag

PriceTag is a self-service LLM gateway platform for OpenShift deployments.
This repository is the deployment and operations home for the platform.

## Deploy

Start with the [full OpenShift deployment guide](docs/openshift-deploy-guide.md).
It covers namespace setup, secrets, CloudNativePG, MaaS API integration, gateway
deployment, routes, smoke tests, backups, and recovery.

Deployment inputs are environment-specific. Never commit provider keys,
database passwords, session secrets, kubeconfigs, or generated API keys.

The intended profiles are:

- `test`: isolated namespace and disposable storage.
- `dogfood`: the team deployment profile.
- `ha`: CloudNativePG, backups, restore, and read-replica operations.

The deploy script preserves existing generated Secrets and ConfigMaps. Set
`ROTATE_SECRETS=true` only for an intentional credential rotation; otherwise a
second run reuses the existing database password, session secret, provider
credentials, and COS credentials.

## Repository Layout

- `deploy/openshift/`: deployment manifests and operational database assets.
- `docs/`: OpenShift deployment and database runbooks.
- `docs/operations/image-provenance.md`: image source and release requirements.
- `tools/`: deployment-adjacent benchmarks and validation tools.
- `SOURCE-MAP.md`: provenance and explicit exclusions.

The metering service and dashboard live in
[`redhat-et/pricetag-metering`](https://github.com/redhat-et/pricetag-metering).
Gateway, MaaS API, and model-server source remain in their upstream projects;
this repository consumes approved image or commit references rather than
vendoring their histories.

## Security

All deployment secrets are supplied at runtime through OpenShift Secrets or
environment variables. Public contributions must pass the repository's full
history secret scan before publication.

## License

Apache License 2.0. See `NOTICE` for third-party attributions.
