# Praxis edge overlay

Deploys Praxis as the edge for the pricetag-test namespace. It uses the shared
application workloads from ../../base, removes their OpenShift Routes, and
exposes Praxis through a LoadBalancer on 80 and 443. HTTP-01 challenges are
forwarded to the OpenShift router.

## Files

| File | Purpose |
| --- | --- |
| kustomization.yaml | Combines this overlay with ../../base, generates the Praxis ConfigMap, removes the old Routes and edge resources, and selects image tags. |
| namespace.yaml | Creates pricetag-test. |
| maas-api-rbac-binding.yaml | Grants the MaaS API service account its cluster role. |
| praxis-edge-config.yaml | Praxis listeners, TLS, redirects, and path routing. |
| praxis-deployment.yaml | Praxis Deployment and public LoadBalancer Service. |
| praxis-certificate.yaml | Requests one certificate for the gateway and dashboard hosts. |
| network-policy.yaml | Default-deny policy and the traffic allowances needed by this stack. |

## Apply

Before applying, make sure the required images, application Secrets/ConfigMaps,
cert-manager, and the letsencrypt-prod ClusterIssuer are available. Set the
hostnames and provider values for your environment:

    export GATEWAY_HOST='ai-gateway.example.test'
    export DASHBOARD_HOST='dashboard.example.test'
    export LEGACY_GATEWAY_HOST="$GATEWAY_HOST"
    export GATEWAY_URL="https://$GATEWAY_HOST"
    export QWEN_ENDPOINT='your-qwen-endpoint.example'
    export CB_GLM_ENDPOINT='your-glm-endpoint.example'
    export VERTEX_PROJECT='your-gcp-project'

The NetworkPolicies also need the cluster DNS and API addresses:

    export KUBE_DNS_SERVICE_IP="$(oc -n openshift-dns get service dns-default -o jsonpath='{.spec.clusterIP}')"
    export KUBE_API_SERVICE_IP="$(oc -n default get service kubernetes -o jsonpath='{.spec.clusterIP}')"
    export KUBE_API_ENDPOINT_IP="$(oc -n default get endpoints kubernetes -o jsonpath='{.subsets[0].addresses[0].ip}')"

From the repository root, render, substitute, review, and apply:

    oc kustomize deploy/openshift/overlays/praxis-edge > /tmp/praxis-edge.yaml
    envsubst '${GATEWAY_HOST} ${DASHBOARD_HOST} ${LEGACY_GATEWAY_HOST} ${GATEWAY_URL} ${QWEN_ENDPOINT} ${CB_GLM_ENDPOINT} ${VERTEX_PROJECT} ${KUBE_DNS_SERVICE_IP} ${KUBE_API_SERVICE_IP} ${KUBE_API_ENDPOINT_IP}' < /tmp/praxis-edge.yaml > /tmp/praxis-edge-rendered.yaml
    oc apply -f /tmp/praxis-edge-rendered.yaml

The explicit envsubst list preserves Praxis runtime placeholders such as
${host}, ${path}, and ${query}.
