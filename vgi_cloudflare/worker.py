"""The Cloudflare VGI worker: catalog assembly and entry points.

ATTACH 'cloudflare' AS cf (TYPE vgi,
  LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-cloudflare vgi-cloudflare');
CREATE SECRET cf (TYPE cloudflare, api_token '<token>');
"""

from __future__ import annotations

import os
import sys

from vgi import Worker

from .catalog import build_catalog, merge_resources
from .generated import GENERATED_RESOURCES
from .graphql import graphql_catalog_items
from .items import ITEM_RESOURCES
from .resources import CORE_RESOURCES
from .secret import CLOUDFLARE_SECRET_SPEC

DATA_VERSION = "0.1.0"
GIT_COMMIT = os.environ.get("VGI_CLOUDFLARE_GIT_COMMIT") or "unknown"

# Hand-written resources take precedence over the OpenAPI-generated ones.
_ALL_RESOURCES = merge_resources(CORE_RESOURCES + ITEM_RESOURCES, GENERATED_RESOURCES)
CLOUDFLARE_CATALOG = build_catalog(_ALL_RESOURCES, extra={"analytics": graphql_catalog_items()})


class CloudflareWorker(Worker):
    """Worker hosting the Cloudflare catalog."""

    catalog = CLOUDFLARE_CATALOG
    secret_types = [CLOUDFLARE_SECRET_SPEC]


def main() -> None:
    """Run the worker over stdio (DuckDB spawns it as a subprocess)."""
    CloudflareWorker.main()


def main_http() -> None:
    """Run the worker as an HTTP server (Docker / Fly.io): ``main`` with ``--http``."""
    argv = sys.argv[1:]
    if "--http" not in argv:
        argv = ["--http", *argv]
    sys.argv = [sys.argv[0], *argv]
    CloudflareWorker.main()
