# Image Provenance

The current dogfood images are cluster-internal artifacts. They are evidence
of the running environment, not valid fresh-deployment inputs.

| Component | Current dogfood digest | Rebuild source | Fresh-deployment status |
|---|---|---|---|
| Praxis | `sha256:abba2f05...` | `dogfood-adopted-9be8880c` | Publish an external image from the adopted source tree. |
| MaaS API | `sha256:f6359f85...` | Pin an approved `models-as-a-service` revision. | Rebuild and publish an immutable image. |
| Metering | `sha256:05e9d104...` | `redhat-et/pricetag-metering` | Build from a tagged repository commit. |

Do not copy the internal registry references into a new cluster deployment.
Every release must record the source commit, image digest, and build system
that produced the image. A digest from an untraceable binary build is not
reproducible provenance.

The intended external image locations are:

- `quay.io/redhat-et/pricetag-gateway`
- `quay.io/redhat-et/pricetag-maas-api`
- `quay.io/redhat-et/pricetag-metering`

The image-publishing workflow and release tags must be added before claiming
that a bare OpenShift deployment is fully self-contained.
