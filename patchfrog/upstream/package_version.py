"""Package version comparison (M6.3).

A version change is *evidence of risk*, never proof of breakage:

- major bump (or a 0.x minor bump, per semver's pre-1.0 rule) ->
  ``POTENTIALLY_BREAKING``;
- minor bump -> ``NON_BREAKING``; patch bump -> ``NON_BREAKING``;
- downgrade / a pre-release target -> ``POTENTIALLY_BREAKING``;
- anything unparseable -> ``UNKNOWN``.

Only structural evidence (a contract or SDK-surface diff) can make a
change ``BREAKING``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from patchfrog.upstream.domain import (
    CompatibilityClass,
    ContractDiffItem,
    DiffItemKind,
    DiffSubject,
    SubjectKind,
)

_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[.\-+]?((?:a|b|rc|alpha|beta|pre|dev)[\w.]*))?", re.IGNORECASE)


@dataclass(frozen=True, slots=True, order=True)
class ParsedVersion:
    major: int
    minor: int
    patch: int
    prerelease: str | None = None

    @property
    def text(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}-{self.prerelease}" if self.prerelease else base


def parse_version(value: str | None) -> ParsedVersion | None:
    """The first version-looking token in a version or a declared spec:
    ``1.2.3``, ``v2.0.0``, ``==12.3.0``, ``^13.0.0``, ``>=1.4,<2``."""

    if not value:
        return None
    match = _VERSION_RE.search(value)
    if match is None:
        return None
    major, minor, patch, pre = match.groups()
    return ParsedVersion(int(major), int(minor or 0), int(patch or 0), pre.lower() if pre else None)


def version_diff_items(package: str, old: str | None, new: str | None) -> tuple[ContractDiffItem, ...]:
    if not new or (old and old.strip() == new.strip()):
        return ()
    subject = DiffSubject(SubjectKind.PACKAGE, name=package)
    location = f"package[{package}].version"
    old_v, new_v = parse_version(old), parse_version(new)
    if old_v is None or new_v is None:
        return (
            ContractDiffItem(
                DiffItemKind.PACKAGE_VERSION_UNPARSEABLE, location, subject, CompatibilityClass.UNKNOWN, old, new,
                f"{package} version changed ({old or 'unknown'} -> {new}); versions not comparable",
                (f"old.version={old or 'unknown'}", f"new.version={new}"), replacement=new,
            ),
        )
    evidence = (f"old.version={old_v.text}", f"new.version={new_v.text}")
    if (new_v.major, new_v.minor, new_v.patch) < (old_v.major, old_v.minor, old_v.patch):
        return (
            ContractDiffItem(DiffItemKind.PACKAGE_DOWNGRADE, location, subject, CompatibilityClass.POTENTIALLY_BREAKING,
                             old, new, f"{package} downgraded {old_v.text} -> {new_v.text}", evidence, replacement=new),
        )
    if new_v.major > old_v.major:
        return (
            ContractDiffItem(
                DiffItemKind.PACKAGE_MAJOR_BUMP, location, subject, CompatibilityClass.POTENTIALLY_BREAKING, old, new,
                f"{package} major version bump {old_v.text} -> {new_v.text}; semver permits breaking changes",
                (*evidence, "semver.major"), replacement=new,
            ),
        )
    if new_v.minor > old_v.minor:
        pre_one = old_v.major == 0
        return (
            ContractDiffItem(
                DiffItemKind.PACKAGE_MINOR_BUMP, location, subject,
                CompatibilityClass.POTENTIALLY_BREAKING if pre_one else CompatibilityClass.NON_BREAKING, old, new,
                f"{package} minor version bump {old_v.text} -> {new_v.text}"
                + ("; pre-1.0 minor bumps may break" if pre_one else ""),
                (*evidence, "semver.minor", *(("semver.pre_1_0",) if pre_one else ())), replacement=new,
            ),
        )
    if new_v.prerelease and new_v.prerelease != old_v.prerelease:
        return (
            ContractDiffItem(DiffItemKind.PACKAGE_PRERELEASE_CHANGE, location, subject,
                             CompatibilityClass.POTENTIALLY_BREAKING, old, new,
                             f"{package} moves to pre-release {new_v.text}", (*evidence, "semver.prerelease"),
                             replacement=new),
        )
    if (new_v.major, new_v.minor, new_v.patch) == (old_v.major, old_v.minor, old_v.patch):
        return ()
    return (
        ContractDiffItem(DiffItemKind.PACKAGE_PATCH_BUMP, location, subject, CompatibilityClass.NON_BREAKING, old, new,
                         f"{package} patch version bump {old_v.text} -> {new_v.text}", (*evidence, "semver.patch"),
                         replacement=new),
    )


__all__ = ["ParsedVersion", "parse_version", "version_diff_items"]
