"""Content-addressed cache of Tree-sitter parse results.

Keyed by ``(content_hash, language)`` rather than by repository or commit
— parsing depends only on a file's bytes and language, so a file whose
content is unchanged (same hash) never needs Tree-sitter re-invoked for
it again, whether that's because of an incremental re-index of the same
repository or because identical content happens to appear elsewhere. This
is the mechanism behind "unchanged files are not reparsed" (see
:mod:`patchfrog.indexing.service`).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from patchfrog.domain.code import Language
from patchfrog.persistence.models._enum import enum_column
from patchfrog.persistence.models.base import Base


class ParsedFileCacheModel(Base):
    """A JSON-serialized :class:`~patchfrog.domain.code.ParsedFile` payload."""

    __tablename__ = "parsed_file_cache"

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    language: Mapped[Language] = mapped_column(enum_column(Language, length=16), primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
