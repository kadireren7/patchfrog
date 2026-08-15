"""Persisted repository-intelligence tables for one indexing run.

Every table here is scoped by ``repository_index_id`` and cascades on its
deletion — an index version's rows are a self-contained unit, so deleting
(or simply abandoning) one index never touches another version's data.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Boolean, Uuid

from patchfrog.domain.code import ImportKind, Language, SymbolKind
from patchfrog.intelligence.graph import EdgeKind
from patchfrog.intelligence.resolution import ResolutionStatus
from patchfrog.persistence.models._enum import enum_column
from patchfrog.persistence.models.base import Base


class FileIndexStatus(StrEnum):
    """Outcome of attempting to parse one inventoried file."""

    PARSED = "parsed"
    FAILED = "failed"
    SKIPPED = "skipped"


class IndexedFileModel(Base):
    """One file inventoried by a repository indexing run."""

    __tablename__ = "indexed_files"
    __table_args__ = (
        Index("uq_indexed_files_index_path", "repository_index_id", "relative_path", unique=True),
        Index("ix_indexed_files_index_content_hash", "repository_index_id", "content_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repository_index_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("repository_indexes.id", ondelete="CASCADE"), index=True
    )
    relative_path: Mapped[str] = mapped_column(String(1024))
    language: Mapped[Language | None] = mapped_column(enum_column(Language, length=16), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    git_blob_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_test: Mapped[bool] = mapped_column(Boolean)
    is_generated: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[FileIndexStatus] = mapped_column(enum_column(FileIndexStatus, length=16))
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class SymbolModel(Base):
    """One structural symbol extracted from an :class:`IndexedFileModel`."""

    __tablename__ = "symbols"
    __table_args__ = (
        Index("ix_symbols_index_qualified_name", "repository_index_id", "qualified_name"),
        Index("ix_symbols_index_name", "repository_index_id", "name"),
        Index("ix_symbols_file_span", "indexed_file_id", "start_line", "end_line"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repository_index_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("repository_indexes.id", ondelete="CASCADE"), index=True
    )
    indexed_file_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("indexed_files.id", ondelete="CASCADE"), index=True
    )
    parent_symbol_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("symbols.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(512))
    qualified_name: Mapped[str] = mapped_column(String(2048))
    kind: Mapped[SymbolKind] = mapped_column(enum_column(SymbolKind, length=16))
    language: Mapped[Language] = mapped_column(enum_column(Language, length=16))
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    start_column: Mapped[int] = mapped_column(Integer)
    end_column: Mapped[int] = mapped_column(Integer)
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    visibility: Mapped[str | None] = mapped_column(String(32), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))


class ImportReferenceModel(Base):
    """One ``import``/``#include`` found in an :class:`IndexedFileModel`."""

    __tablename__ = "import_references"
    __table_args__ = (
        Index("ix_import_references_resolved_file", "resolved_file_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repository_index_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("repository_indexes.id", ondelete="CASCADE"), index=True
    )
    indexed_file_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("indexed_files.id", ondelete="CASCADE"), index=True
    )
    resolved_file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("indexed_files.id", ondelete="CASCADE"), nullable=True
    )
    raw_text: Mapped[str] = mapped_column(Text)
    target: Mapped[str] = mapped_column(String(2048))
    kind: Mapped[ImportKind] = mapped_column(enum_column(ImportKind, length=16))
    line: Mapped[int] = mapped_column(Integer)


class CallReferenceModel(Base):
    """One call site found in an :class:`IndexedFileModel`.

    ``resolution_status`` and ``resolved_symbol_id`` are never guessed —
    an ambiguous or unresolvable callee is persisted as such, never
    silently mapped to one of several candidates. See
    :mod:`patchfrog.intelligence.resolution`.
    """

    __tablename__ = "call_references"
    __table_args__ = (
        Index("ix_call_references_caller_symbol", "caller_symbol_id"),
        Index("ix_call_references_resolved_symbol", "resolved_symbol_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repository_index_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("repository_indexes.id", ondelete="CASCADE"), index=True
    )
    indexed_file_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("indexed_files.id", ondelete="CASCADE"), index=True
    )
    caller_symbol_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("symbols.id", ondelete="CASCADE"), nullable=True
    )
    resolved_symbol_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("symbols.id", ondelete="CASCADE"), nullable=True
    )
    callee_name: Mapped[str] = mapped_column(String(512))
    line: Mapped[int] = mapped_column(Integer)
    column: Mapped[int] = mapped_column(Integer)
    resolution_status: Mapped[ResolutionStatus] = mapped_column(enum_column(ResolutionStatus, length=16))


class RepositoryEdgeModel(Base):
    """One explicit, queryable graph edge — the resolved closure over
    containment, imports/includes, calls, and test-relationship heuristics
    for one indexing run. Built by :func:`patchfrog.intelligence.graph.build_graph`.
    """

    __tablename__ = "repository_edges"
    __table_args__ = (
        Index("ix_repository_edges_index_kind", "repository_index_id", "kind"),
        Index("ix_repository_edges_source_file", "source_file_id"),
        Index("ix_repository_edges_target_file", "target_file_id"),
        Index("ix_repository_edges_source_symbol", "source_symbol_id"),
        Index("ix_repository_edges_target_symbol", "target_symbol_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repository_index_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("repository_indexes.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[EdgeKind] = mapped_column(enum_column(EdgeKind, length=32))
    source_file_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("indexed_files.id", ondelete="CASCADE"))
    source_symbol_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("symbols.id", ondelete="CASCADE"), nullable=True
    )
    target_file_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("indexed_files.id", ondelete="CASCADE"))
    target_symbol_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("symbols.id", ondelete="CASCADE"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
