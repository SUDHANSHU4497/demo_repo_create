"""Rule interfaces and the per-table context that caches expensive backend calls."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import cached_property

from agentic_devops.config import ValidationConfig
from agentic_devops.models import Finding
from agentic_devops.validation.backends import ColumnInfo, KeyStats, TableBackend, TableProfile
from agentic_devops.validation.contracts import DataContract
from agentic_devops.validation.naming import NamingStandards


@dataclass
class TableContext:
    contract: DataContract
    fq_name: str
    backend: TableBackend
    config: ValidationConfig
    naming: NamingStandards
    _key_stats: dict[tuple[str, ...], KeyStats] = field(default_factory=dict, repr=False)

    @property
    def display_name(self) -> str:
        return self.contract.table

    @cached_property
    def schema(self) -> list[ColumnInfo]:
        return self.backend.get_schema(self.fq_name)

    @cached_property
    def column_names(self) -> list[str]:
        return [c.name for c in self.schema]

    @cached_property
    def profile(self) -> TableProfile:
        """Row count + null counts for every column, computed in a single pass."""
        return self.backend.profile(
            self.fq_name, self.column_names, self.config.treat_blank_as_null, self.contract.filter
        )

    def key_stats(self, keys: list[str]) -> KeyStats:
        k = tuple(keys)
        if k not in self._key_stats:
            self._key_stats[k] = self.backend.key_stats(
                self.fq_name, list(keys), self.contract.filter, self.config.sample_limit
            )
        return self._key_stats[k]

    def actual_column(self, name: str) -> str | None:
        """Case-insensitive lookup of a column in the physical schema."""
        lname = name.lower()
        for c in self.column_names:
            if c.lower() == lname:
                return c
        return None


class Rule(ABC):
    """A check over one table. ``requires_data`` rules scan rows; others only read the schema."""

    rule_id: str = "rule"
    requires_data: bool = True

    @abstractmethod
    def evaluate(self, ctx: TableContext) -> list[Finding]: ...


class CrossTableRule(ABC):
    rule_id: str = "cross_table"

    @abstractmethod
    def evaluate(
        self, contexts: list[TableContext], reference_contracts: list[DataContract], naming: NamingStandards
    ) -> list[Finding]: ...
