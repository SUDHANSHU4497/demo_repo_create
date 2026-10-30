from agentic_devops.validation.rules.base import CrossTableRule, Rule, TableContext
from agentic_devops.validation.rules.column_naming import ColumnNamingRule, CrossTableNamingRule
from agentic_devops.validation.rules.null_columns import NullColumnsRule
from agentic_devops.validation.rules.primary_key import PrimaryKeyRule
from agentic_devops.validation.rules.schema_contract import SchemaContractRule


def default_rules() -> list[Rule]:
    return [SchemaContractRule(), ColumnNamingRule(), PrimaryKeyRule(), NullColumnsRule()]


def default_cross_table_rules() -> list[CrossTableRule]:
    return [CrossTableNamingRule()]


__all__ = [
    "ColumnNamingRule",
    "CrossTableNamingRule",
    "CrossTableRule",
    "NullColumnsRule",
    "PrimaryKeyRule",
    "Rule",
    "SchemaContractRule",
    "TableContext",
    "default_cross_table_rules",
    "default_rules",
]
