"""Shared helper for mapping a :class:`~enum.StrEnum` onto a SQL column."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum as SAEnum


def enum_column(enum_cls: type[StrEnum], *, length: int) -> SAEnum:
    """A non-native ``Enum`` column storing each member's ``.value``.

    ``native_enum=False`` keeps this portable across SQLite (used in
    tests) and PostgreSQL. ``values_callable`` is required — without it
    SQLAlchemy persists the member *name* (e.g. ``"SUCCEEDED"``) instead
    of its value (``"succeeded"``), silently diverging from what the
    Alembic migration declares and from what raw SQL against the table
    expects.
    """

    return SAEnum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
    )
