# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "vgi-python[http,haybarn]>=0.40.0",
#     "vgi-rpc>=0.48.0",
#     "httpx>=0.27",
#     "pyarrow>=20",
# ]
# ///
"""Stdio entry point for the Cloudflare VGI worker (``uv run``).

ATTACH 'cloudflare' AS cf (TYPE vgi, LOCATION 'uv run cloudflare_worker.py');
CREATE SECRET cf (TYPE cloudflare, api_token '<token>');
"""

from __future__ import annotations

from vgi_cloudflare.worker import CloudflareWorker, main

__all__ = ["CloudflareWorker", "main"]

if __name__ == "__main__":
    main()
