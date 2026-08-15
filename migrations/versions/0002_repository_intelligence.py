"""Repository intelligence: indexes, files, symbols, imports, calls, edges

Revision ID: 0002_repository_intelligence
Revises: 0001_initial
Create Date: 2026-08-15

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_repository_intelligence"
down_revision: str | None = "0001_initial"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_LANGUAGE = sa.Enum("python", "c", "cpp", name="language", native_enum=False, length=16)
_SYMBOL_KIND = sa.Enum(
    "module", "function", "method", "class", "struct", "enum", "union", "interface",
    "variable", "constant", "type_alias", "macro",
    name="symbol_kind", native_enum=False, length=16,
)
_IMPORT_KIND = sa.Enum("local", "external", name="import_kind", native_enum=False, length=16)
_RESOLUTION_STATUS = sa.Enum(
    "resolved", "unresolved", "ambiguous", name="resolution_status", native_enum=False, length=16
)
_EDGE_KIND = sa.Enum(
    "file_imports_file", "file_includes_file", "symbol_contains_symbol", "symbol_calls_symbol",
    "symbol_references_symbol", "file_tests_file", "symbol_tested_by_symbol",
    name="edge_kind", native_enum=False, length=32,
)


def upgrade() -> None:
    op.create_table(
        "repository_indexes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("commit_sha", sa.String(length=40), nullable=False),
        sa.Column("index_version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "running", "succeeded", "failed", name="index_status", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("files_total", sa.Integer(), nullable=False),
        sa.Column("files_parsed", sa.Integer(), nullable=False),
        sa.Column("files_failed", sa.Integer(), nullable=False),
        sa.Column("files_reused", sa.Integer(), nullable=False),
        sa.Column("symbols_extracted", sa.Integer(), nullable=False),
        sa.Column("edges_created", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_repository_indexes_repository_id", "repository_indexes", ["repository_id"])
    op.create_index(
        "uq_repository_indexes_repo_version", "repository_indexes", ["repository_id", "index_version"],
        unique=True,
    )
    op.create_index(
        "uq_repository_indexes_active_per_repo", "repository_indexes", ["repository_id"],
        unique=True, postgresql_where=sa.text("is_active = true"), sqlite_where=sa.text("is_active = 1"),
    )

    op.create_table(
        "indexed_files",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_index_id", sa.Uuid(), nullable=False),
        sa.Column("relative_path", sa.String(length=1024), nullable=False),
        sa.Column("language", _LANGUAGE, nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("git_blob_sha", sa.String(length=40), nullable=True),
        sa.Column("is_test", sa.Boolean(), nullable=False),
        sa.Column("is_generated", sa.Boolean(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("parsed", "failed", "skipped", name="file_index_status", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["repository_index_id"], ["repository_indexes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_indexed_files_repository_index_id", "indexed_files", ["repository_index_id"])
    op.create_index(
        "uq_indexed_files_index_path", "indexed_files", ["repository_index_id", "relative_path"], unique=True
    )
    op.create_index(
        "ix_indexed_files_index_content_hash", "indexed_files", ["repository_index_id", "content_hash"]
    )

    op.create_table(
        "symbols",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_index_id", sa.Uuid(), nullable=False),
        sa.Column("indexed_file_id", sa.Uuid(), nullable=False),
        sa.Column("parent_symbol_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("qualified_name", sa.String(length=2048), nullable=False),
        sa.Column("kind", _SYMBOL_KIND, nullable=False),
        sa.Column("language", _LANGUAGE, nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("start_column", sa.Integer(), nullable=False),
        sa.Column("end_column", sa.Integer(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=True),
        sa.Column("visibility", sa.String(length=32), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["repository_index_id"], ["repository_indexes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["indexed_file_id"], ["indexed_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_symbol_id"], ["symbols.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_symbols_repository_index_id", "symbols", ["repository_index_id"])
    op.create_index("ix_symbols_indexed_file_id", "symbols", ["indexed_file_id"])
    op.create_index("ix_symbols_index_qualified_name", "symbols", ["repository_index_id", "qualified_name"])
    op.create_index("ix_symbols_index_name", "symbols", ["repository_index_id", "name"])
    op.create_index("ix_symbols_file_span", "symbols", ["indexed_file_id", "start_line", "end_line"])

    op.create_table(
        "import_references",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_index_id", sa.Uuid(), nullable=False),
        sa.Column("indexed_file_id", sa.Uuid(), nullable=False),
        sa.Column("resolved_file_id", sa.Uuid(), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("target", sa.String(length=2048), nullable=False),
        sa.Column("kind", _IMPORT_KIND, nullable=False),
        sa.Column("line", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["repository_index_id"], ["repository_indexes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["indexed_file_id"], ["indexed_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resolved_file_id"], ["indexed_files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_import_references_repository_index_id", "import_references", ["repository_index_id"])
    op.create_index("ix_import_references_indexed_file_id", "import_references", ["indexed_file_id"])
    op.create_index("ix_import_references_resolved_file", "import_references", ["resolved_file_id"])

    op.create_table(
        "call_references",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_index_id", sa.Uuid(), nullable=False),
        sa.Column("indexed_file_id", sa.Uuid(), nullable=False),
        sa.Column("caller_symbol_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_symbol_id", sa.Uuid(), nullable=True),
        sa.Column("callee_name", sa.String(length=512), nullable=False),
        sa.Column("line", sa.Integer(), nullable=False),
        sa.Column("column", sa.Integer(), nullable=False),
        sa.Column("resolution_status", _RESOLUTION_STATUS, nullable=False),
        sa.ForeignKeyConstraint(["repository_index_id"], ["repository_indexes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["indexed_file_id"], ["indexed_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["caller_symbol_id"], ["symbols.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resolved_symbol_id"], ["symbols.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_call_references_repository_index_id", "call_references", ["repository_index_id"])
    op.create_index("ix_call_references_indexed_file_id", "call_references", ["indexed_file_id"])
    op.create_index("ix_call_references_caller_symbol", "call_references", ["caller_symbol_id"])
    op.create_index("ix_call_references_resolved_symbol", "call_references", ["resolved_symbol_id"])

    op.create_table(
        "repository_edges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_index_id", sa.Uuid(), nullable=False),
        sa.Column("kind", _EDGE_KIND, nullable=False),
        sa.Column("source_file_id", sa.Uuid(), nullable=False),
        sa.Column("source_symbol_id", sa.Uuid(), nullable=True),
        sa.Column("target_file_id", sa.Uuid(), nullable=False),
        sa.Column("target_symbol_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.String(length=256), nullable=True),
        sa.ForeignKeyConstraint(["repository_index_id"], ["repository_indexes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_file_id"], ["indexed_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_file_id"], ["indexed_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_symbol_id"], ["symbols.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_symbol_id"], ["symbols.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_repository_edges_repository_index_id", "repository_edges", ["repository_index_id"])
    op.create_index("ix_repository_edges_index_kind", "repository_edges", ["repository_index_id", "kind"])
    op.create_index("ix_repository_edges_source_file", "repository_edges", ["source_file_id"])
    op.create_index("ix_repository_edges_target_file", "repository_edges", ["target_file_id"])
    op.create_index("ix_repository_edges_source_symbol", "repository_edges", ["source_symbol_id"])
    op.create_index("ix_repository_edges_target_symbol", "repository_edges", ["target_symbol_id"])

    op.create_table(
        "parsed_file_cache",
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("language", _LANGUAGE, nullable=False),
        sa.Column("parser_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("content_hash", "language", "parser_version"),
    )


def downgrade() -> None:
    op.drop_table("parsed_file_cache")

    op.drop_index("ix_repository_edges_target_symbol", table_name="repository_edges")
    op.drop_index("ix_repository_edges_source_symbol", table_name="repository_edges")
    op.drop_index("ix_repository_edges_target_file", table_name="repository_edges")
    op.drop_index("ix_repository_edges_source_file", table_name="repository_edges")
    op.drop_index("ix_repository_edges_index_kind", table_name="repository_edges")
    op.drop_index("ix_repository_edges_repository_index_id", table_name="repository_edges")
    op.drop_table("repository_edges")

    op.drop_index("ix_call_references_resolved_symbol", table_name="call_references")
    op.drop_index("ix_call_references_caller_symbol", table_name="call_references")
    op.drop_index("ix_call_references_indexed_file_id", table_name="call_references")
    op.drop_index("ix_call_references_repository_index_id", table_name="call_references")
    op.drop_table("call_references")

    op.drop_index("ix_import_references_resolved_file", table_name="import_references")
    op.drop_index("ix_import_references_indexed_file_id", table_name="import_references")
    op.drop_index("ix_import_references_repository_index_id", table_name="import_references")
    op.drop_table("import_references")

    op.drop_index("ix_symbols_file_span", table_name="symbols")
    op.drop_index("ix_symbols_index_name", table_name="symbols")
    op.drop_index("ix_symbols_index_qualified_name", table_name="symbols")
    op.drop_index("ix_symbols_indexed_file_id", table_name="symbols")
    op.drop_index("ix_symbols_repository_index_id", table_name="symbols")
    op.drop_table("symbols")

    op.drop_index("ix_indexed_files_index_content_hash", table_name="indexed_files")
    op.drop_index("uq_indexed_files_index_path", table_name="indexed_files")
    op.drop_index("ix_indexed_files_repository_index_id", table_name="indexed_files")
    op.drop_table("indexed_files")

    op.drop_index("uq_repository_indexes_active_per_repo", table_name="repository_indexes")
    op.drop_index("uq_repository_indexes_repo_version", table_name="repository_indexes")
    op.drop_index("ix_repository_indexes_repository_id", table_name="repository_indexes")
    op.drop_table("repository_indexes")
