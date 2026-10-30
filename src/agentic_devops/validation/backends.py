"""Compute backends for validation rules.

Rules never touch Spark directly. They ask a backend for a few aggregate facts (schema, row
count, per-column null counts, key statistics). This keeps rules unit-testable with pandas and
lets the Spark backend compute everything in as few passes over the data as possible:

* one aggregation pass for row count + null counts of every column
* one shuffle (group by key) for primary-key duplicate detection
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

_FLOAT_TYPES = {"double", "float"}


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    data_type: str


@dataclass
class TableProfile:
    row_count: int
    null_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class KeyStats:
    row_count: int
    null_key_rows: int
    duplicate_key_groups: int
    duplicate_rows: int  # rows that share a key with at least one other row
    sample_duplicates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def distinct_keys(self) -> int:
        non_null = self.row_count - self.null_key_rows
        return non_null - (self.duplicate_rows - self.duplicate_key_groups)


class TableBackend(ABC):
    @abstractmethod
    def table_exists(self, table: str) -> bool: ...

    @abstractmethod
    def get_schema(self, table: str) -> list[ColumnInfo]: ...

    @abstractmethod
    def profile(
        self, table: str, columns: list[str], blank_as_null: bool, row_filter: str | None
    ) -> TableProfile: ...

    @abstractmethod
    def key_stats(
        self, table: str, key_columns: list[str], row_filter: str | None, sample_limit: int
    ) -> KeyStats: ...


def _quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


class SparkBackend(TableBackend):
    """Runs on Databricks. ``spark`` is the active SparkSession."""

    def __init__(self, spark: Any):
        self.spark = spark

    def _df(self, table: str, row_filter: str | None):
        df = self.spark.table(table)
        if row_filter:
            df = df.where(row_filter)
        return df

    def table_exists(self, table: str) -> bool:
        return bool(self.spark.catalog.tableExists(table))

    def get_schema(self, table: str) -> list[ColumnInfo]:
        return [ColumnInfo(f.name, f.dataType.simpleString()) for f in self.spark.table(table).schema.fields]

    def profile(self, table, columns, blank_as_null, row_filter) -> TableProfile:
        from pyspark.sql import functions as F

        df = self._df(table, row_filter)
        types = {f.name: f.dataType.simpleString() for f in df.schema.fields}
        exprs = [F.count(F.lit(1)).alias("__row_count")]
        for i, name in enumerate(columns):
            col = F.col(_quote(name))
            cond = col.isNull()
            dtype = types.get(name, "")
            if blank_as_null and dtype == "string":
                cond = cond | (F.trim(col) == "")
            elif dtype in _FLOAT_TYPES:
                cond = cond | F.isnan(col)
            exprs.append(F.sum(F.when(cond, 1).otherwise(0)).alias(f"__n{i}"))
        row = df.agg(*exprs).collect()[0]
        return TableProfile(
            row_count=int(row["__row_count"]),
            null_counts={name: int(row[f"__n{i}"] or 0) for i, name in enumerate(columns)},
        )

    def key_stats(self, table, key_columns, row_filter, sample_limit) -> KeyStats:
        from functools import reduce

        from pyspark import StorageLevel
        from pyspark.sql import functions as F

        df = self._df(table, row_filter)
        keys = [F.col(_quote(k)) for k in key_columns]
        null_cond = reduce(lambda a, b: a | b, [k.isNull() for k in keys])
        totals = df.agg(
            F.count(F.lit(1)).alias("rows"), F.sum(F.when(null_cond, 1).otherwise(0)).alias("null_rows")
        ).collect()[0]
        dups = (
            df.where(~null_cond)
            .groupBy(*keys)
            .agg(F.count(F.lit(1)).alias("__dup_count"))
            .where(F.col("__dup_count") > 1)
        ).persist(StorageLevel.MEMORY_AND_DISK)
        try:
            agg = dups.agg(F.count(F.lit(1)).alias("groups"), F.sum("__dup_count").alias("rows")).collect()[0]
            groups = int(agg["groups"] or 0)
            samples: list[dict[str, Any]] = []
            if groups and sample_limit > 0:
                for r in dups.orderBy(F.col("__dup_count").desc()).limit(sample_limit).collect():
                    d = r.asDict()
                    count = d.pop("__dup_count")
                    samples.append({"key": {k: _jsonable(v) for k, v in d.items()}, "count": int(count)})
        finally:
            dups.unpersist()
        return KeyStats(
            row_count=int(totals["rows"]),
            null_key_rows=int(totals["null_rows"] or 0),
            duplicate_key_groups=groups,
            duplicate_rows=int(agg["rows"] or 0),
            sample_duplicates=samples,
        )


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


_PANDAS_TYPE_MAP = {
    "int64": "bigint",
    "Int64": "bigint",
    "int32": "int",
    "Int32": "int",
    "float64": "double",
    "Float64": "double",
    "float32": "float",
    "bool": "boolean",
    "boolean": "boolean",
    "object": "string",
    "string": "string",
    "str": "string",
}


class PandasBackend(TableBackend):
    """In-memory backend for unit tests and small local checks.

    ``tables`` maps fully-qualified names to pandas DataFrames. ``schemas`` can override the
    reported data types (pandas cannot represent e.g. ``decimal(18,2)``). SQL row filters are
    Spark-specific and are ignored here.
    """

    def __init__(self, tables: dict[str, Any], schemas: dict[str, dict[str, str]] | None = None):
        self.tables = {k.lower(): v for k, v in tables.items()}
        self.schemas = {k.lower(): v for k, v in (schemas or {}).items()}

    def _get(self, table: str):
        return self.tables[table.lower()]

    def table_exists(self, table: str) -> bool:
        return table.lower() in self.tables

    def get_schema(self, table: str) -> list[ColumnInfo]:
        df = self._get(table)
        override = self.schemas.get(table.lower(), {})
        out = []
        for name in df.columns:
            if name in override:
                out.append(ColumnInfo(name, override[name]))
                continue
            dtype = str(df[name].dtype)
            mapped = "timestamp" if dtype.startswith("datetime64") else _PANDAS_TYPE_MAP.get(dtype, dtype)
            out.append(ColumnInfo(name, mapped))
        return out

    def profile(self, table, columns, blank_as_null, row_filter) -> TableProfile:
        if row_filter:
            log.debug("PandasBackend ignores row filter %r", row_filter)
        df = self._get(table)
        counts = {}
        for name in columns:
            s = df[name]
            mask = s.isna()
            if blank_as_null:  # works for object and the pandas>=3 "str" dtype
                mask = mask | s.map(lambda v: isinstance(v, str) and v.strip() == "")
            counts[name] = int(mask.sum())
        return TableProfile(row_count=len(df), null_counts=counts)

    def key_stats(self, table, key_columns, row_filter, sample_limit) -> KeyStats:
        df = self._get(table)
        null_mask = df[key_columns].isna().any(axis=1)
        non_null = df[~null_mask]
        sizes = non_null.groupby(key_columns, dropna=False).size()
        dups = sizes[sizes > 1].sort_values(ascending=False)
        samples = []
        for key, count in list(dups.items())[:sample_limit]:
            values = key if isinstance(key, tuple) else (key,)
            samples.append(
                {
                    "key": {k: _jsonable(_py(v)) for k, v in zip(key_columns, values, strict=False)},
                    "count": int(count),
                }
            )
        return KeyStats(
            row_count=len(df),
            null_key_rows=int(null_mask.sum()),
            duplicate_key_groups=int(len(dups)),
            duplicate_rows=int(dups.sum()) if len(dups) else 0,
            sample_duplicates=samples,
        )


def _py(value: Any) -> Any:
    return value.item() if hasattr(value, "item") else value
