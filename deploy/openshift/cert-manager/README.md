# cert-manager bootstrap (one-time, per cluster)

Cluster-scoped prerequisites for the public EnMaaS hostnames. NOT part of the
kustomize build (the overlay is namespaced; these are cluster-scoped and
idempotent), and not run by `deploy.sh`.

Needed because the cluster's default router certificate only covers
`*.apps.rosa.<cluster>...`. Public `devshift.net` hostnames therefore fail TLS
verification (`ERR_TLS_CERT_ALTNAME_INVALID`) until a matching certificate is
attached to the Routes - see `../overlays/enmaas/tls.yaml` for the
Certificates and the router RBAC that consume this issuer.

## Why HTTP-01 and not DNS-01

The `devshift.net` zone lives in the **app-sre** AWS account and is managed in
app-interface (`data/aws/app-sre/dns/devshift.net.yaml`). This cluster has no
credentials to write ACME TXT records there, so DNS-01 is not an option.
HTTP-01 only needs the public CNAME that already points at this router;
cert-manager creates a temporary Ingress, which OpenShift converts to a Route
on port 80 for the challenge.

## Apply

```bash
oc apply -f 00-operator.yaml
oc -n cert-manager rollout status deploy/cert-manager --timeout=300s
oc apply -f 01-clusterissuer.yaml
oc get clusterissuer letsencrypt-prod -o jsonpath='{.status.conditions[*].message}'
# expect: The ACME account was registered with the ACME server
```

Then the namespaced Certificates come from the overlay
(`kustomize build deploy/openshift/overlays/enmaas`), and issuance is
automatic. Renewal is at 60 days with no human involvement; verify with:

```bash
oc get certificate -n enmaas
openssl s_client -connect api.enmaas.devshift.net:443 \
  -servername api.enmaas.devshift.net </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -dates
```
