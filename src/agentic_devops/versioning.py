"""Semantic versioning from Conventional Commit messages (``feat:`` -> minor, ``fix:`` -> patch, ``!``/BREAKING -> major)."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_BREAKING = re.compile(r"^\w+(\([^)]*\))?!:|BREAKING[ -]CHANGE", re.MULTILINE)
_FEAT = re.compile(r"^(?:Merged PR \d+:\s*)?feat(\([^)]*\))?:", re.IGNORECASE | re.MULTILINE)
BUMPS = ("major", "minor", "patch")


@dataclass(frozen=True, order=True)
class SemVer:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str, prefix: str = "v") -> SemVer | None:
        t = text.strip()
        if prefix and t.startswith(prefix):
            t = t[len(prefix) :]
        m = _SEMVER.match(t)
        return cls(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None

    def bump(self, kind: str) -> SemVer:
        if kind == "major":
            return SemVer(self.major + 1, 0, 0)
        if kind == "minor":
            return SemVer(self.major, self.minor + 1, 0)
        if kind == "patch":
            return SemVer(self.major, self.minor, self.patch + 1)
        raise ValueError(f"Unknown bump {kind!r}")

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def determine_bump(messages: list[str]) -> str:
    if any(_BREAKING.search(m) for m in messages):
        return "major"
    if any(_FEAT.search(m) for m in messages):
        return "minor"
    return "patch"


def latest_version(tag_names: list[str], prefix: str = "v") -> SemVer | None:
    versions = [v for v in (SemVer.parse(t, prefix) for t in tag_names) if v]
    return max(versions) if versions else None


def next_version(
    current: SemVer | None, messages: list[str], bump: str = "auto", initial: str = "0.1.0"
) -> SemVer:
    if current is None:
        first = SemVer.parse(initial, prefix="")
        if first is None:
            raise ValueError(f"Invalid initial version {initial!r}")
        return first
    kind = determine_bump(messages) if bump == "auto" else bump
    if kind not in BUMPS:
        raise ValueError(f"bump must be auto|major|minor|patch, got {bump!r}")
    return current.bump(kind)
