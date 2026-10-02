"""Hand-written descriptors for the core Cloudflare resources.

These establish the pattern and stay authoritative for high-value tables with
clean, curated schemas. The OpenAPI codegen (tools/generate_resources.py)
produces additional descriptors of the same shape for the long tail.
"""

from __future__ import annotations

import pyarrow as pa

from .descriptor import Column, Example, Pagination, PathParam, QueryParam, ResourceDescriptor

_TS = pa.timestamp("us", tz="UTC")

ZONES = ResourceDescriptor(
    name="zones",
    schema="zones",
    path="/zones",
    description="Zones (domains) accessible to the API token.",
    categories=("cloudflare", "core"),
    pagination=Pagination.PAGE,
    cardinality_estimate=50,
    cardinality_max=10_000,
    query_params=(
        QueryParam("name", pa.string(), "Filter by exact zone name (domain)."),
        QueryParam("status", pa.string(), "Filter by zone status (active, pending, ...)."),
        QueryParam("account_id", pa.string(), "Filter by account id.", api_name="account.id"),
    ),
    columns=(
        Column("id", pa.string(), "Zone id (primary key / join key).", nullable=False),
        Column("name", pa.string(), "Zone name (domain).", nullable=False),
        Column("status", pa.string(), "Zone status: active, pending, initializing, moved, deleted."),
        Column("paused", pa.bool_(), "Whether the zone is paused (Cloudflare not proxying)."),
        Column("type", pa.string(), "Zone type: full, partial (CNAME setup), secondary."),
        Column("development_mode", pa.int64(), "Seconds remaining in development mode (0 = off)."),
        Column("account_id", pa.string(), "Owning account id.", source="account.id"),
        Column("account_name", pa.string(), "Owning account name.", source="account.name"),
        Column("name_servers", pa.string(), "Assigned Cloudflare name servers (JSON array)."),
        Column("created_on", _TS, "When the zone was created.", source="created_on"),
        Column("modified_on", _TS, "When the zone was last modified.", source="modified_on"),
        Column("activated_on", _TS, "When the zone was activated.", source="activated_on"),
    ),
    examples=(
        Example(
            "SELECT id, name, status FROM cloudflare.zones.zones",
            "List every zone the token can see, with its activation status.",
        ),
        Example(
            "SELECT * FROM cloudflare.zones.zones WHERE status = 'active'",
            "Only active zones: the status filter is pushed to the API query string.",
        ),
    ),
)

ACCOUNTS = ResourceDescriptor(
    name="accounts",
    schema="accounts",
    path="/accounts",
    description="Accounts the API token can access.",
    categories=("cloudflare", "core"),
    pagination=Pagination.PAGE,
    cardinality_estimate=5,
    cardinality_max=1_000,
    query_params=(QueryParam("name", pa.string(), "Filter by account name."),),
    columns=(
        Column("id", pa.string(), "Account id (primary key / join key).", nullable=False),
        Column("name", pa.string(), "Account name.", nullable=False),
        Column("type", pa.string(), "Account type (standard, enterprise, ...)."),
        Column(
            "enforce_twofactor",
            pa.bool_(),
            "Whether 2FA is enforced for members.",
            source="settings.enforce_twofactor",
        ),
        Column("created_on", _TS, "When the account was created."),
    ),
    examples=(
        Example(
            "SELECT id, name FROM cloudflare.accounts.accounts",
            "List the accounts the token can access.",
        ),
    ),
)

ACCOUNT_MEMBERS = ResourceDescriptor(
    name="members",
    schema="accounts",
    path="/accounts/{account_id}/members",
    description="Members of an account, with their roles and invitation status.",
    categories=("cloudflare", "iam"),
    pagination=Pagination.PAGE,
    cardinality_estimate=20,
    cardinality_max=100_000,
    path_params=(PathParam("account_id", "Account id whose members to list."),),
    query_params=(QueryParam("status", pa.string(), "Filter by membership status (accepted, pending)."),),
    columns=(
        Column("id", pa.string(), "Membership id.", nullable=False),
        Column("status", pa.string(), "Membership status: accepted, pending, rejected."),
        Column(
            "user_id",
            pa.string(),
            "Id of the member's Cloudflare user (the same across every account).",
            source="user.id",
        ),
        Column("email", pa.string(), "User email.", source="user.email"),
        Column("first_name", pa.string(), "User first name.", source="user.first_name"),
        Column("last_name", pa.string(), "User last name.", source="user.last_name"),
        Column(
            "two_factor_enabled",
            pa.bool_(),
            "Whether the user has 2FA enabled.",
            source="user.two_factor_authentication_enabled",
        ),
        Column("roles", pa.string(), "Assigned roles (JSON array of role objects)."),
    ),
    examples=(
        Example(
            "SELECT email, status FROM cloudflare.accounts.members WHERE account_id = '<acct>'",
            "Members of one account; account_id is required and maps to the URL.",
        ),
    ),
)

DNS_RECORDS = ResourceDescriptor(
    name="records",
    schema="dns",
    path="/zones/{zone_id}/dns_records",
    description="DNS records in a zone: name, type, content, TTL, and whether Cloudflare proxies them.",
    categories=("cloudflare", "dns"),
    pagination=Pagination.PAGE,
    cardinality_estimate=200,
    cardinality_max=10_000_000,
    path_params=(PathParam("zone_id", "Zone id whose DNS records to list."),),
    query_params=(
        QueryParam("type", pa.string(), "Filter by record type (A, AAAA, CNAME, TXT, MX, ...)."),
        QueryParam("name", pa.string(), "Filter by exact record name."),
        QueryParam("content", pa.string(), "Filter by exact record content."),
        QueryParam("proxied", pa.bool_(), "Filter by whether the record is proxied."),
    ),
    columns=(
        Column("id", pa.string(), "DNS record id (primary key).", nullable=False),
        Column("name", pa.string(), "Record name (FQDN).", nullable=False),
        Column("type", pa.string(), "Record type: A, AAAA, CNAME, TXT, MX, NS, ...", nullable=False),
        Column("content", pa.string(), "Record content (the value)."),
        Column("proxiable", pa.bool_(), "Whether the record can be proxied by Cloudflare."),
        Column("proxied", pa.bool_(), "Whether Cloudflare proxies traffic for this record."),
        Column("ttl", pa.int64(), "Time-to-live in seconds (1 = automatic)."),
        Column("priority", pa.int64(), "Priority (MX/SRV records)."),
        Column("locked", pa.bool_(), "Whether the record is locked."),
        Column("comment", pa.string(), "User comment on the record."),
        Column("created_on", _TS, "When the record was created."),
        Column("modified_on", _TS, "When the record was last modified."),
    ),
    examples=(
        Example(
            "SELECT name, type, content FROM cloudflare.dns.records WHERE zone_id = '<zone>'",
            "All DNS records in one zone; zone_id is required and maps to the URL.",
        ),
        Example(
            "SELECT * FROM cloudflare.dns.records WHERE zone_id = '<zone>' AND type = 'A'",
            "Only A records: the type filter is pushed to the API query string.",
        ),
    ),
)

#: Hand-written resources, in catalog order.
CORE_RESOURCES: tuple[ResourceDescriptor, ...] = (
    ZONES,
    ACCOUNTS,
    ACCOUNT_MEMBERS,
    DNS_RECORDS,
)
