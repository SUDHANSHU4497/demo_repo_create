"""Column naming standards and helpers (snake_case conversion, abbreviation canonicalisation)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agentic_devops.models import Severity

SNAKE_CASE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_CAMEL_1 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_2 = re.compile(r"([a-z0-9])([A-Z])")
_NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")


@dataclass
class TypeConvention:
    """E.g. boolean columns should start with ``is_``/``has_``; timestamps should end with ``_ts``."""

    data_type: str
    prefixes: list[str] = field(default_factory=list)
    suffixes: list[str] = field(default_factory=list)
    severity: Severity = Severity.WARNING

    def matches(self, name: str) -> bool:
        lname = name.lower()
        if self.prefixes and any(lname.startswith(p) for p in self.prefixes):
            return True
        return bool(self.suffixes and any(lname.endswith(s) for s in self.suffixes))


@dataclass
class NamingStandards:
    case_style: str = "snake_case"
    max_length: int = 64
    reserved_words: set[str] = field(default_factory=set)
    abbreviations: dict[str, str] = field(default_factory=dict)
    allowed_tokens: set[str] = field(default_factory=set)
    type_conventions: list[TypeConvention] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> NamingStandards:
        conventions = [
            TypeConvention(
                data_type=str(c["type"]).lower(),
                prefixes=[str(p).lower() for p in c.get("prefixes") or []],
                suffixes=[str(s).lower() for s in c.get("suffixes") or []],
                severity=Severity(str(c.get("severity", "warning")).lower()),
            )
            for c in d.get("type_conventions") or []
        ]
        return cls(
            case_style=str(d.get("case_style", "snake_case")),
            max_length=int(d.get("max_length", 64)),
            reserved_words={str(w).lower() for w in d.get("reserved_words") or []},
            abbreviations={str(k).lower(): str(v).lower() for k, v in (d.get("abbreviations") or {}).items()},
            allowed_tokens={str(t).lower() for t in d.get("allowed_tokens") or []},
            type_conventions=conventions,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_style": self.case_style,
            "max_length": self.max_length,
            "reserved_words": sorted(self.reserved_words),
            "abbreviations": dict(self.abbreviations),
            "allowed_tokens": sorted(self.allowed_tokens),
            "type_conventions": [
                {
                    "type": c.data_type,
                    "prefixes": c.prefixes,
                    "suffixes": c.suffixes,
                    "severity": c.severity.value,
                }
                for c in self.type_conventions
            ],
        }

    def canonical_name(self, name: str) -> str:
        """snake_case the name and expand known abbreviations: ``CustID`` -> ``customer_id``."""
        tokens = to_snake_case(name).split("_")
        out = []
        for t in tokens:
            if t in self.allowed_tokens:
                out.append(t)
            else:
                out.append(self.abbreviations.get(t, t))
        return "_".join(x for x in out if x)

    def abbreviation_tokens(self, name: str) -> list[str]:
        return [
            t
            for t in to_snake_case(name).split("_")
            if t in self.abbreviations and t not in self.allowed_tokens
        ]


def load_naming_standards(path: str | Path | None) -> NamingStandards:
    if not path or not Path(path).exists():
        return NamingStandards()
    with open(path, encoding="utf-8") as fh:
        return NamingStandards.from_dict(yaml.safe_load(fh) or {})


def to_snake_case(name: str) -> str:
    """``Customer ID`` / ``customerId`` / ``CUSTOMER-ID`` -> ``customer_id``."""
    s = name.strip()
    s = _CAMEL_1.sub(r"\1_\2", s)
    s = _CAMEL_2.sub(r"\1_\2", s)
    s = _NON_ALNUM.sub("_", s).strip("_").lower()
    s = re.sub(r"_+", "_", s)
    if s and s[0].isdigit():
        s = "c_" + s
    return s or "unnamed_column"


def describe_case_problems(name: str) -> list[str]:
    """Human-readable reasons a name is not snake_case."""
    problems = []
    if name != name.strip():
        problems.append("has leading/trailing whitespace")
    if re.search(r"\s", name.strip()):
        problems.append("contains whitespace")
    specials = sorted(set(re.findall(r"[^0-9A-Za-z_\s]", name)))
    if specials:
        problems.append("contains special characters " + " ".join(f"'{c}'" for c in specials))
    if name.strip()[:1].isdigit():
        problems.append("starts with a digit")
    if re.search(r"[A-Z]", name):
        problems.append("uses upper-case letters (camelCase/PascalCase/UPPER)")
    if "__" in name:
        problems.append("contains a double underscore")
    if name.strip().startswith("_") or name.strip().endswith("_"):
        problems.append("starts or ends with an underscore")
    return problems or ["does not match snake_case"]


_TYPE_ALIASES = {
    "long": "bigint",
    "integer": "int",
    "short": "smallint",
    "byte": "tinyint",
    "real": "float",
    "bool": "boolean",
    "str": "string",
    "text": "string",
    "timestamp_ltz": "timestamp",
}


def normalize_type(data_type: str | None) -> str | None:
    if not data_type:
        return None
    t = re.sub(r"\s+", "", data_type.strip().lower())
    if t.startswith("varchar") or t.startswith("char("):
        return "string"
    return _TYPE_ALIASES.get(t, t)


def type_family(data_type: str | None) -> str | None:
    """``decimal(18,2)`` -> ``decimal``; used to match type conventions."""
    t = normalize_type(data_type)
    return t.split("(")[0].split("<")[0] if t else None
