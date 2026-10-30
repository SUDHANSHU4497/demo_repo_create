"""Data contracts: the declared shape of each table (primary key, columns, owners, source code paths).

Contracts live in git next to the code (``contracts/<domain>/<table>.yml``) so a PR that changes a
pipeline and its contract is reviewed together. ``sources`` globs map changed files to the tables
they write, which is how the Data Validation Agent decides which tables a PR affects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml


class ContractError(ValueError):
    pass


@dataclass
class ColumnSpec:
    name: str
    type: str | None = None
    nullable: bool = True
    allow_fully_null: bool = False
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "nullable": self.nullable,
            "allow_fully_null": self.allow_fully_null,
            "description": self.description,
        }


@dataclass
class DataContract:
    table: str  # "<schema>.<table>"; the catalog is injected per environment
    primary_key: list[str] = field(default_factory=list)
    columns: list[ColumnSpec] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    owner: str | None = None
    description: str | None = None
    filter: str | None = None  # optional Spark SQL predicate to bound scans of very large tables
    min_rows: int = 0
    allow_extra_columns: bool = True
    path: str | None = None

    def fq_name(self, catalog: str) -> str:
        parts = self.table.split(".")
        if len(parts) == 3:
            return self.table
        return f"{catalog}.{self.table}"

    def column(self, name: str) -> ColumnSpec | None:
        lname = name.lower()
        for c in self.columns:
            if c.name.lower() == lname:
                return c
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "primary_key": list(self.primary_key),
            "columns": [c.to_dict() for c in self.columns],
            "sources": list(self.sources),
            "owner": self.owner,
            "description": self.description,
            "validation": {
                "filter": self.filter,
                "min_rows": self.min_rows,
                "allow_extra_columns": self.allow_extra_columns,
            },
            "path": self.path,
        }


def contract_from_dict(data: dict[str, Any], path: str | None = None) -> DataContract:
    if not isinstance(data, dict) or not data.get("table"):
        raise ContractError(f"Contract {path or ''} must define 'table'")
    table = str(data["table"]).strip()
    if len(table.split(".")) not in (2, 3):
        raise ContractError(f"Contract {path or table}: 'table' must be schema.table or catalog.schema.table")
    pk = data.get("primary_key") or []
    if isinstance(pk, str):
        pk = [pk]
    columns = []
    for c in data.get("columns") or []:
        if isinstance(c, str):
            columns.append(ColumnSpec(name=c))
            continue
        if not c.get("name"):
            raise ContractError(f"Contract {path or table}: every column needs a 'name'")
        columns.append(
            ColumnSpec(
                name=str(c["name"]),
                type=str(c["type"]) if c.get("type") else None,
                nullable=bool(c.get("nullable", True)),
                allow_fully_null=bool(c.get("allow_fully_null", False)),
                description=c.get("description"),
            )
        )
    v = data.get("validation") or {}
    return DataContract(
        table=table,
        primary_key=[str(k) for k in pk],
        columns=columns,
        sources=[str(s) for s in data.get("sources") or []],
        owner=data.get("owner"),
        description=data.get("description"),
        filter=v.get("filter"),
        min_rows=int(v.get("min_rows", 0)),
        allow_extra_columns=bool(v.get("allow_extra_columns", True)),
        path=path or data.get("path"),
    )


def load_contract(path: str | Path) -> DataContract:
    p = Path(path)
    with open(p, encoding="utf-8") as fh:
        return contract_from_dict(yaml.safe_load(fh), path=p.as_posix())


def load_contracts(directory: str | Path) -> list[DataContract]:
    root = Path(directory)
    if not root.exists():
        return []
    contracts = [load_contract(p) for p in sorted(root.rglob("*.yml")) + sorted(root.rglob("*.yaml"))]
    seen: dict[str, str] = {}
    for c in contracts:
        key = c.table.lower()
        if key in seen:
            raise ContractError(f"Duplicate contract for table {c.table}: {seen[key]} and {c.path}")
        seen[key] = c.path or ""
    return contracts


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def resolve_impacted_contracts(
    contracts: list[DataContract],
    changed_files: list[str],
    global_impact_paths: list[str] | None = None,
) -> list[DataContract]:
    """Return contracts whose table is (or may be) affected by the changed files.

    A contract is impacted when its own file changed or a changed file matches one of its
    ``sources`` globs. A change under a ``global_impact_paths`` glob (shared libraries, naming
    standards) impacts every contract.
    """
    files = [_norm(f) for f in changed_files]
    if any(fnmatch(f, _norm(g)) for f in files for g in global_impact_paths or []):
        return list(contracts)
    impacted = []
    for c in contracts:
        own = _norm(c.path) if c.path else None
        if own and any(f == own or f.endswith("/" + own) or own.endswith("/" + f) for f in files):
            impacted.append(c)
            continue
        if any(fnmatch(f, _norm(src)) for f in files for src in c.sources):
            impacted.append(c)
    return impacted
