# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "vgi-python[http,haybarn,oauth]>=0.40.0",
#     "vgi-rpc[sentry]>=0.48.0",
#     "httpx>=0.27",
#     "pyarrow>=20",
# ]
# ///
"""HTTP entry point for the Cloudflare VGI worker (used by Docker / Fly.io)."""

from __future__ import annotations

from vgi_cloudflare.worker import main_http

if __name__ == "__main__":
    main_http()
