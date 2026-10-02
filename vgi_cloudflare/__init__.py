"""vgi-cloudflare — expose the Cloudflare REST API to DuckDB as SQL tables via VGI.

Layout:
  descriptor.py  — declarative ResourceDescriptor model (one per Cloudflare list endpoint)
  client.py      — httpx-based Cloudflare API client (auth, pagination, error handling)
  runtime.py     — make_resource_function(): turns a descriptor into a VGI TableFunctionGenerator
  secret.py      — the ``cloudflare`` DuckDB secret type (Bearer api_token)
  resources.py   — hand-written descriptors for the core resources
  catalog.py     — assembles descriptors into a VGI Catalog + Worker

The entry point is ../cloudflare_worker.py.
"""

__version__ = "0.1.0"
