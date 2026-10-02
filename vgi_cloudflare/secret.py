"""The ``cloudflare`` DuckDB secret type.

Users authenticate by creating a secret once per DuckDB session:

    CREATE SECRET cf (
        TYPE cloudflare,
        api_token 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
    );

The worker advertises this secret type at ATTACH (see catalog.py), and each
table function declares ``required_secrets`` so the framework resolves it and
passes it through ``params.secrets["cloudflare"]`` at process time.
"""

from __future__ import annotations

import pyarrow as pa
from vgi.catalog.secret_type import SecretTypeSpec

#: The DuckDB secret type name and the key holding the Cloudflare API token.
CLOUDFLARE_SECRET_TYPE = "cloudflare"
API_TOKEN_KEY = "api_token"

#: Optional legacy auth: Global API Key + account email (X-Auth-Key / X-Auth-Email).
API_KEY_KEY = "api_key"
API_EMAIL_KEY = "email"

CLOUDFLARE_SECRET_SPEC = SecretTypeSpec(
    name=CLOUDFLARE_SECRET_TYPE,
    description="Cloudflare API credentials. Provide either api_token (preferred) "
    "or api_key + email (legacy Global API Key).",
    schema=pa.schema(
        [
            pa.field(API_TOKEN_KEY, pa.string(), metadata={"redact": "true"}),
            pa.field(API_KEY_KEY, pa.string(), metadata={"redact": "true"}),
            pa.field(API_EMAIL_KEY, pa.string()),
        ]
    ),
)
