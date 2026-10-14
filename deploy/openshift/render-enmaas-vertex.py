#!/usr/bin/env python3
"""Inject EnMaaS-only Vertex fragments into the rendered Praxis ConfigMap."""

from pathlib import Path
import os
import re
import sys


def main() -> int:
    rendered_path = Path(sys.argv[1])
    fragments_dir = Path(sys.argv[2])
    rendered = rendered_path.read_text()
    fragments = {
        "MODEL_TO_PROVIDER_FILTER": "intelligent-route-unified.yaml",
        "ROUTER_ROUTE": "router-route.yaml",
        "FILTER": "vertex-filter.yaml",
        "GCP_CREDENTIAL_FILTER": "gcp-credential-filter.yaml",
        "HOST_OVERRIDE": "vertex-host-override.yaml",
        "UPSTREAM_CLUSTER": "vertex-cluster.yaml",
        "ONLY_MODEL_CATALOG": "overlay-model-catalog.yaml",
        "METERING_INTERNAL_AUTH": "metering-internal-auth.yaml",
        "OPENAI_PATH_REWRITE": "openai-path-rewrite.yaml",
        "OPENAI_GCP_CREDENTIAL_FILTER": "gcp-credential-filter.yaml",
        "OPENAI_HOST_OVERRIDE": "vertex-host-override.yaml",
        "OPENAI_UPSTREAM_CLUSTER": "vertex-cluster.yaml",
        "OPENAI_MODEL_TO_PROVIDER_FILTER": "intelligent-route-openai.yaml",
        "OPENAI_ROUTER_ROUTE": "router-route.yaml",
    }

    vertex_only = os.environ.get("VERTEX_ONLY", "true")
    if vertex_only not in {"true", "false"}:
        raise ValueError("VERTEX_ONLY must be true or false")

    # Remove the legacy router blocks before fragment replacement removes the
    # marker comments that delimit the blocks. The provider-specific path
    # rewrite and credential fragments must remain in the chain.
    for chain_name, next_marker in (
        ("openai", "ENMAAS_VERTEX_OPENAI_PATH_REWRITE"),
        ("unified", "ENMAAS_VERTEX_GCP_CREDENTIAL_FILTER"),
    ):
        chain = re.search(
            rf"(?ms)^      - name: {chain_name}\n        filters:\n(?P<body>.*?)(?=^      - name: |\Z)",
            rendered,
        )
        if chain is None:
            raise ValueError(f"could not find {chain_name} filter chain")
        body, removed = re.subn(
            rf"(?ms)^          - filter: router\n.*?(?=^          # {next_marker}\s*$)",
            "",
            chain.group("body"),
            count=1,
        )
        if removed != 1:
            raise ValueError(f"expected one legacy router in {chain_name}, found {removed}")
        rendered = rendered[: chain.start("body")] + body + rendered[chain.end("body") :]

    # Remove the hand-authored catalog blocks while retaining the first marker
    # as the insertion point for the overlay projection.
    catalog_blocks = re.compile(
        r"(?ms)^          - filter: model_catalog\n.*?(?=^          - filter: |^          # ENMAAS_VERTEX_ONLY_ACCESS\s*$|\Z)"
    )
    rendered, catalog_count = catalog_blocks.subn("", rendered)
    if catalog_count != 2:
        raise ValueError(f"expected two static model catalogs, found {catalog_count}")

    for marker, filename in fragments.items():
        pattern = re.compile(rf"(?m)^(?P<indent>[ \t]*)# ENMAAS_VERTEX_{marker}\s*$")
        # The dogfood profile serves the complete shared catalog and access
        # policy, while EnMaaS uses the corresponding markers to narrow its
        # public catalog to Vertex models. Keep one fragment set, but remove
        # the narrowing fragments from the dogfood render.
        if marker == "ONLY_MODEL_CATALOG":
            snippet = (fragments_dir / "overlay-model-catalog.yaml").read_text().rstrip("\n").splitlines()
        elif marker == "MODEL_TO_PROVIDER_FILTER":
            snippet = (fragments_dir / "intelligent-route-unified.yaml").read_text().rstrip("\n").splitlines()
        elif marker == "OPENAI_MODEL_TO_PROVIDER_FILTER":
            snippet = (fragments_dir / "intelligent-route-openai.yaml").read_text().rstrip("\n").splitlines()
        elif marker in {"ROUTER_ROUTE", "OPENAI_ROUTER_ROUTE"}:
            snippet = []
        else:
            snippet = (fragments_dir / filename).read_text().rstrip("\n").splitlines()

        def replace(match: re.Match[str]) -> str:
            indent = match.group("indent")
            return "\n".join(f"{indent}{line}" if line else indent for line in snippet)

        rendered, replacements = pattern.subn(replace, rendered)
        if marker in {"ROUTER_ROUTE", "OPENAI_ROUTER_ROUTE"}:
            expected = 0
        else:
            expected = 3 if marker == "METERING_INTERNAL_AUTH" else 1
        if replacements != expected:
            raise ValueError(f"expected {expected} insertion markers for {marker}, found {replacements}")

    model_policy_check = os.environ.get("METERING_MODEL_POLICY_CHECK", "false")
    if model_policy_check not in {"true", "false"}:
        raise ValueError("METERING_MODEL_POLICY_CHECK must be true or false")
    policy_pattern = re.compile(r"(?m)^(?P<indent>[ \t]*)# ENMAAS_METERING_MODEL_POLICY\s*$")

    def replace_model_policy(match: re.Match[str]) -> str:
        if model_policy_check == "true":
            return f'{match.group("indent")}model_policy_check: true'
        return ""

    rendered, policy_replacements = policy_pattern.subn(replace_model_policy, rendered)
    if policy_replacements != 3:
        raise ValueError(f"expected three metering model-policy markers, found {policy_replacements}")

    # Praxis serves /metrics, /ready and /healthy on the admin listener and
    # refuses any non-loopback bind unless insecure_options.allow_public_admin
    # is set. Prometheus can only scrape it over the pod network, so EnMaaS can
    # opt in; the validators require the 9901 ingress policy whenever it does.
    public_admin = os.environ.get("PRAXIS_PUBLIC_ADMIN", "false")
    if public_admin not in {"true", "false"}:
        raise ValueError("PRAXIS_PUBLIC_ADMIN must be true or false")
    if public_admin == "true":
        admin_pattern = re.compile(r'(?m)^(?P<indent>[ \t]*)address: "127\.0\.0\.1:9901"\s*$')
        rendered, admin_replacements = admin_pattern.subn(
            lambda match: f'{match.group("indent")}address: "0.0.0.0:9901"', rendered
        )
        if admin_replacements != 1:
            raise ValueError(f"expected one Praxis admin address, found {admin_replacements}")
        options_pattern = re.compile(r"(?m)^(?P<indent>[ \t]*)insecure_options: \{\}\s*$")
        rendered, options_replacements = options_pattern.subn(
            lambda match: f'{match.group("indent")}insecure_options:\n{match.group("indent")}  allow_public_admin: true',
            rendered,
        )
        if options_replacements != 1:
            raise ValueError(f"expected one empty Praxis insecure_options block, found {options_replacements}")

    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, IndexError) as error:
        print(f"render-enmaas-vertex: {error}", file=sys.stderr)
        raise SystemExit(1) from error
