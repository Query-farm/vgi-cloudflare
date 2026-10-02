"""Prototype get-one-by-id ("item") resources.

These are exposed as table *functions* whose required arguments are the URL path
params, in URL order. Each returns 0 or 1 row (0 when the API returns
``result: null`` / 404). They shine in correlated/lateral joins:

    SELECT z.name, r.content
    FROM cf.zones.zones z, cf.dns.record(z.id, '<record-id>') r;

Hand-written here to validate the pattern before generating the full set from
the OpenAPI spec.
"""

from __future__ import annotations

import pyarrow as pa

from .descriptor import Column, Example, Pagination, PathParam, ResourceDescriptor

_TS = pa.timestamp("us", tz="UTC")

# 0 path params — account/zone-less singleton (GET /user).
USER = ResourceDescriptor(
    name="user",
    schema="accounts",
    path="/user",
    kind="item",
    pagination=Pagination.NONE,
    description="The user owning the API token (GET /user).",
    categories=("cloudflare", "core", "item"),
    columns=(
        Column("id", pa.string(), "User id.", nullable=False),
        Column("email", pa.string(), "User email."),
        Column("first_name", pa.string(), "First name on the token owner's Cloudflare profile."),
        Column("last_name", pa.string(), "Last name on the token owner's Cloudflare profile."),
        Column("username", pa.string(), "Cloudflare username of the token owner (distinct from the email)."),
        Column("country", pa.string(), "Country on the token owner's Cloudflare profile."),
        Column("telephone", pa.string(), "Telephone number on the token owner's Cloudflare profile."),
        Column("two_factor_enabled", pa.bool_(), "2FA enabled.", source="two_factor_authentication_enabled"),
        Column("created_on", _TS, "When the user was created."),
        Column("modified_on", _TS, "When the user was last modified."),
    ),
    examples=(
        Example(
            "SELECT email, country FROM cloudflare.accounts.user()",
            "The user that owns the API token.",
        ),
    ),
)

# 1 path param (GET /accounts/{account_id}).
ACCOUNT = ResourceDescriptor(
    name="account",
    schema="accounts",
    path="/accounts/{account_id}",
    kind="item",
    pagination=Pagination.NONE,
    description="A single account by id (GET /accounts/{account_id}).",
    categories=("cloudflare", "core", "item"),
    path_params=(PathParam("account_id", "Account id."),),
    columns=(
        Column("id", pa.string(), "Account id.", nullable=False),
        Column("name", pa.string(), "Account name."),
        Column("type", pa.string(), "Account type."),
        Column("created_on", _TS, "When the account was created."),
    ),
    examples=(
        Example(
            "SELECT * FROM cloudflare.accounts.account('<account-id>')",
            "Look up one account by id.",
        ),
    ),
)

# 1 path param (GET /zones/{zone_id}).
ZONE = ResourceDescriptor(
    name="zone",
    schema="zones",
    path="/zones/{zone_id}",
    kind="item",
    pagination=Pagination.NONE,
    description="A single zone by id (GET /zones/{zone_id}).",
    categories=("cloudflare", "core", "item"),
    path_params=(PathParam("zone_id", "Zone id."),),
    columns=(
        Column("id", pa.string(), "Zone id.", nullable=False),
        Column("name", pa.string(), "Zone name (domain)."),
        Column("status", pa.string(), "Zone status."),
        Column("paused", pa.bool_(), "Whether the zone is paused."),
        Column("type", pa.string(), "Zone type."),
        Column("development_mode", pa.int64(), "Dev-mode seconds remaining."),
        Column("account_id", pa.string(), "Owning account id.", source="account.id"),
        Column("account_name", pa.string(), "Owning account name.", source="account.name"),
        Column("created_on", _TS, "When the zone was created."),
        Column("modified_on", _TS, "When the zone was last modified."),
        Column("activated_on", _TS, "When the zone was activated."),
    ),
    examples=(
        Example(
            "SELECT name, status FROM cloudflare.zones.zone('<zone-id>')",
            "Look up one zone by id; an unknown id returns no rows.",
        ),
    ),
)

# 2 path params, in URL order (GET /zones/{zone_id}/dns_records/{dns_record_id}).
DNS_RECORD = ResourceDescriptor(
    name="record",
    schema="dns",
    path="/zones/{zone_id}/dns_records/{dns_record_id}",
    kind="item",
    pagination=Pagination.NONE,
    description="A single DNS record (GET /zones/{zone_id}/dns_records/{dns_record_id}).",
    categories=("cloudflare", "dns", "item"),
    path_params=(
        PathParam("zone_id", "Zone id."),
        PathParam("dns_record_id", "DNS record id."),
    ),
    columns=(
        Column("id", pa.string(), "DNS record id.", nullable=False),
        Column("name", pa.string(), "Record name (FQDN)."),
        Column("type", pa.string(), "Record type."),
        Column("content", pa.string(), "Record content."),
        Column("proxied", pa.bool_(), "Whether Cloudflare proxies this record."),
        Column("proxiable", pa.bool_(), "Whether the record can be proxied."),
        Column("ttl", pa.int64(), "Time-to-live (seconds)."),
        Column("comment", pa.string(), "User comment."),
        Column("created_on", _TS, "When the record was created."),
        Column("modified_on", _TS, "When the record was last modified."),
    ),
    examples=(
        Example(
            "SELECT name, type, content FROM cloudflare.dns.record('<zone-id>', '<record-id>')",
            "Look up one DNS record by zone and record id.",
        ),
    ),
)

#: Prototype item resources, in catalog order.
ITEM_RESOURCES: tuple[ResourceDescriptor, ...] = (USER, ACCOUNT, ZONE, DNS_RECORD)
