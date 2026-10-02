"""Load the OpenAPI-generated resource descriptors from resources_generated.json.

The manifest is produced offline by tools/generate_resources.py and committed.
This module maps its compact type-name strings back to Arrow types and builds
``ResourceDescriptor`` objects — the same shape as the hand-written resources,
so they flow through the identical runtime.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa

from .descriptor import Column, Pagination, PathParam, QueryParam, ResourceDescriptor

_MANIFEST = Path(__file__).resolve().parent / "resources_generated.json"

_TS = pa.timestamp("us", tz="UTC")
_ARROW_TYPES: dict[str, pa.DataType] = {
    "string": pa.string(),
    "int64": pa.int64(),
    "float64": pa.float64(),
    "bool": pa.bool_(),
    "timestamp": _TS,
}


def _arrow(name: str) -> pa.DataType:
    return _ARROW_TYPES.get(name, pa.string())


def _load() -> tuple[ResourceDescriptor, ...]:
    if not _MANIFEST.exists():
        return ()
    data = json.loads(_MANIFEST.read_text())
    out: list[ResourceDescriptor] = []
    for d in data.get("resources", []):
        out.append(
            ResourceDescriptor(
                name=d["name"],
                path=d["path"],
                schema=d.get("schema", "misc"),
                kind=d.get("kind", "list"),
                result_path=d.get("result_path", "result"),
                columns=tuple(
                    Column(
                        name=c["name"],
                        type=_arrow(c["type"]),
                        doc=c.get("doc", ""),
                        source=c.get("source"),
                    )
                    for c in d["columns"]
                ),
                path_params=tuple(
                    PathParam(p["name"], p.get("doc", ""), tuple(p.get("choices", ())), p.get("pattern"))
                    for p in d["path_params"]
                ),
                query_params=tuple(
                    QueryParam(
                        name=q["name"],
                        type=_arrow(q["type"]),
                        doc=q.get("doc", ""),
                        api_name=q.get("api_name"),
                        choices=tuple(q.get("choices", ())),
                        ge=q.get("ge"),
                        le=q.get("le"),
                        pattern=q.get("pattern"),
                    )
                    for q in d["query_params"]
                ),
                pagination=Pagination(d.get("pagination", "page")),
                description=d.get("description", ""),
                doc=d.get("doc", ""),
                api_tag=d.get("api_tag", ""),
                area=d.get("area", ""),
                default_query=tuple(d.get("default_query", {}).items()),
                categories=tuple(d.get("categories", ())),
            )
        )
    return tuple(out)


#: Descriptors generated from the Cloudflare OpenAPI spec.
GENERATED_RESOURCES: tuple[ResourceDescriptor, ...] = _load()
