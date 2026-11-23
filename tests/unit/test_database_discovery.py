"""SQLAlchemy / SQLModel discovery (Milestone 2.4): determinism, schema
compatibility, limits, scale, cancellation, revision binding, malformed
input, path safety, secrets, and the no-import/no-connection guarantee. The
pattern corpus is test_database_corpus.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tracemalloc
from collections.abc import Mapping
from pathlib import Path

import pytest

from patchfrog.app.discovery import DISCOVERY_ARTIFACT, run_discovery
from patchfrog.app.jobs import CancellationToken, JobRunner
from patchfrog.app.revision import capture_revision
from patchfrog.app.store import LocalRunStore, RunState
from patchfrog.scalability.project import ApplicationModel, DiscoveryLimits, discover_application
from patchfrog.scalability.project.db_domain import (
    DatabaseModel,
    DbUnresolvedKind,
    Provenance,
    TargetResolution,
)

ENGINE = "0.1.0-beta"

MODELS = """
from sqlalchemy import ForeignKey, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
engine = create_engine("postgresql://admin:hunter2@db.example.invalid:5432/prod")
SessionLocal = sessionmaker(bind=engine)
class Base(DeclarativeBase):
    pass
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    posts: Mapped[list["Post"]] = relationship(back_populates="author")
class Post(Base):
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    author: Mapped["User"] = relationship(back_populates="posts")
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
"""


class DictReader:
    def __init__(self, files: Mapping[str, str]) -> None:
        self.files = {k: v.encode() for k, v in files.items()}
        self._skipped: dict[str, str] = {}

    @property
    def skipped(self) -> Mapping[str, str]:
        return self._skipped

    def read(self, path: str) -> bytes | None:
        return self.files.get(path)


def discover(files: Mapping[str, str], limits: DiscoveryLimits | None = None) -> ApplicationModel:
    return discover_application(revision_id="r" * 64, paths=sorted(files), reader=DictReader(files), limits=limits or DiscoveryLimits())


def write_tree(root: Path, files: Mapping[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def synthetic(count: int) -> dict[str, str]:
    files = {"app/__init__.py": "", "app/models/__init__.py": "", "app/base.py": "from sqlalchemy.orm import DeclarativeBase\nclass Base(DeclarativeBase):\n    pass\n"}
    for i in range(count):
        files[f"app/models/m{i}.py"] = (
            "from sqlalchemy import ForeignKey\n"
            "from sqlalchemy.orm import Mapped, mapped_column, relationship\n"
            "from app.base import Base\n"
            f"class M{i}(Base):\n"
            f"    __tablename__ = 't{i}'\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
            + (f"    parent_id: Mapped[int] = mapped_column(ForeignKey('t{i - 1}.id'))\n    parent = relationship('M{i - 1}', lazy='joined')\n" if i else "")
        )
    return files


# --- determinism and schema ---------------------------------------------------------------


def test_output_is_deterministic_and_round_trips() -> None:
    first = discover({"app/models.py": MODELS})
    assert first.to_json_bytes() == discover({"app/models.py": MODELS}).to_json_bytes()
    assert ApplicationModel.from_dict(json.loads(first.to_json_bytes())) == first
    db = first.database
    assert [m.name for m in db.models] == ["Post", "User"]
    assert all(r.resolution is TargetResolution.RESOLVED for m in db.models for r in m.relationships)


def test_version_1_artifacts_still_load_with_an_empty_database_section() -> None:
    data = json.loads(discover({"main.py": "from fastapi import FastAPI\napp = FastAPI()\n"}).to_json_bytes())
    data["schema_version"] = 1
    del data["database"]
    assert ApplicationModel.from_dict(data).database == DatabaseModel.empty()


def test_credentials_in_a_literal_url_are_never_kept(tmp_path: Path) -> None:
    repo = write_tree(tmp_path / "repo", {"app/models.py": MODELS})
    result = run_discovery(LocalRunStore(tmp_path / "store"), repo, engine_version=ENGINE)
    assert result.model is not None
    (engine,) = [o for o in result.model.database.objects if o.kind.value == "engine"]
    assert engine.url_scheme == "postgresql"
    artifact = (tmp_path / "store").rglob("*")
    blob = b"".join(p.read_bytes() for p in artifact if p.is_file())
    for secret in (b"hunter2", b"admin", b"db.example.invalid", b"prod"):
        assert secret not in blob


# --- scale and limits ------------------------------------------------------------------------


def test_large_model_count_is_exact_and_memory_bounded() -> None:
    files = synthetic(1000)
    tracemalloc.start()
    model = discover(files)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    db = model.database
    assert len(db.models) == 1000 and model.coverage.complete
    assert sum(len(m.relationships) for m in db.models) == 999
    assert all(r.resolution is TargetResolution.RESOLVED for m in db.models for r in m.relationships)
    # A generous bound on traced allocations (a regression guard, not a performance claim).
    assert peak < 256 * 1024 * 1024


def test_model_limit_is_explicit() -> None:
    model = discover(synthetic(10), DiscoveryLimits(max_models=4))
    assert len(model.database.models) == 4 and model.coverage.limit_reached
    assert any(x.detail == "max_models" for x in model.limitations)


def test_database_unresolved_limit_is_explicit() -> None:
    body = "from sqlalchemy.orm import DeclarativeBase, relationship\nclass Base(DeclarativeBase):\n    pass\nclass A(Base):\n    __tablename__ = 'a'\n"
    body += "".join(f"    r{i} = relationship('Missing{i}')\n" for i in range(12))
    model = discover({"m.py": body}, DiscoveryLimits(max_unresolved=5))
    assert len(model.database.unresolved) == 5 and model.coverage.limit_reached
    assert any("further database unresolved" in x.detail for x in model.limitations)


# --- malformed input, ambiguity, safety -------------------------------------------------------


def test_malformed_model_file_does_not_hide_other_models() -> None:
    model = discover({"a/models.py": MODELS, "b/broken.py": "from sqlalchemy.orm import Mapped\nclass X(Base:\n"})
    assert [m.name for m in model.database.models] == ["Post", "User"]
    assert [(x.code.value, x.location.path if x.location else None) for x in model.limitations] == [("parse_error", "b/broken.py")]
    assert not model.coverage.complete


def test_ambiguous_base_import_is_reported_not_guessed() -> None:
    base = "from sqlalchemy.orm import DeclarativeBase\nclass Base(DeclarativeBase):\n    pass\n"
    model = discover(
        {
            "db.py": base,
            "svc/db.py": base,
            "svc/models.py": "from sqlalchemy.orm import Mapped, mapped_column\nfrom db import Base\nclass Item(Base):\n    __tablename__ = 'items'\n    id: Mapped[int] = mapped_column(primary_key=True)\n",
        }
    )
    assert model.database.models == ()
    assert [u.kind for u in model.database.unresolved] == [DbUnresolvedKind.MODEL_BASE]
    assert any(u.kind.value == "import" for u in model.unresolved)


def test_duplicate_model_names_in_different_registries_stay_separate() -> None:
    model = discover(
        {
            "one.py": "from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship\nclass B1(DeclarativeBase):\n    pass\nclass User(B1):\n    __tablename__ = 'u1'\n    id: Mapped[int] = mapped_column(primary_key=True)\nclass Note(B1):\n    __tablename__ = 'n1'\n    id: Mapped[int] = mapped_column(primary_key=True)\n    user = relationship('User')\n",
            "two.py": "from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column\nclass B2(DeclarativeBase):\n    pass\nclass User(B2):\n    __tablename__ = 'u2'\n    id: Mapped[int] = mapped_column(primary_key=True)\n",
        }
    )
    note = next(m for m in model.database.models if m.name == "Note")
    assert note.relationships[0].target_model == "model:one:User"
    assert note.relationships[0].resolution is TargetResolution.RESOLVED


def test_model_code_is_never_imported_or_executed(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    payload = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\n"
    repo = write_tree(tmp_path / "repo", {"app/models.py": payload + MODELS, "alembic/env.py": payload + "run_migrations_online()\n"})
    before = set(sys.modules)
    result = run_discovery(LocalRunStore(tmp_path / "store"), repo, engine_version=ENGINE)
    assert not marker.exists()
    assert not {m for m in set(sys.modules) - before if m.startswith(("app", "alembic", "sqlalchemy", "sqlmodel"))}
    assert result.model is not None and len(result.model.database.models) == 2


def test_symlinked_models_outside_the_root_are_ignored(tmp_path: Path) -> None:
    outside = write_tree(tmp_path / "outside", {"models.py": MODELS})
    repo = write_tree(tmp_path / "repo", {"main.py": "x = 1\n"})
    (repo / "models.py").symlink_to(outside / "models.py")
    (repo / "pkg").symlink_to(outside, target_is_directory=True)
    result = run_discovery(LocalRunStore(tmp_path / "store"), repo, engine_version=ENGINE)
    assert result.model is not None and result.model.database.models == ()


def test_model_file_changed_after_capture_is_not_analysed(tmp_path: Path) -> None:
    repo = write_tree(tmp_path / "repo", {"app/models.py": MODELS})
    store = LocalRunStore(tmp_path / "store")

    def capture_then_modify(*args, **kwargs):  # type: ignore[no-untyped-def]
        revision = capture_revision(*args, **kwargs)
        (repo / "app" / "models.py").write_text(MODELS.replace("users", "hacked"))
        return revision

    result = run_discovery(store, repo, engine_version=ENGINE, runner=JobRunner(store, capture=capture_then_modify))
    assert result.job.state is RunState.PARTIAL and result.model is not None
    assert result.model.database.models == ()
    assert any(x.detail == "changed_since_capture" for x in result.model.limitations)


def test_cancellation_during_database_analysis(tmp_path: Path) -> None:
    repo = write_tree(tmp_path / "repo", synthetic(40))
    store = LocalRunStore(tmp_path / "store")
    token = CancellationToken()
    seen = 0
    original = JobRunner.run

    def run_and_cancel(self: JobRunner, spec, job, *, token=None):  # type: ignore[no-untyped-def]
        def wrapped(ctx):  # type: ignore[no-untyped-def]
            nonlocal seen
            inner = ctx.checkpoint

            def checkpoint() -> None:
                nonlocal seen
                seen += 1
                if seen == 120:  # well inside the analysis (after reading, during classification)
                    assert token is not None
                    token.cancel()
                inner()

            ctx.checkpoint = checkpoint
            job(ctx)

        return original(self, spec, wrapped, token=token)

    JobRunner.run = run_and_cancel  # type: ignore[method-assign]
    try:
        result = run_discovery(store, repo, engine_version=ENGINE, token=token)
    finally:
        JobRunner.run = original  # type: ignore[method-assign]
    assert result.job.state is RunState.CANCELLED and result.model is None
    assert DISCOVERY_ARTIFACT not in result.job.record.artifacts


def test_idempotent_and_reproducible_with_the_database_section(tmp_path: Path) -> None:
    repo = write_tree(tmp_path / "repo", {"app/models.py": MODELS})
    store = LocalRunStore(tmp_path / "store")
    first = run_discovery(store, repo, engine_version=ENGINE)
    again = run_discovery(store, repo, engine_version=ENGINE)
    assert again.job.reused and again.model == first.model
    other = run_discovery(LocalRunStore(tmp_path / "s2"), write_tree(tmp_path / "copy" / "repo", {"app/models.py": MODELS}), engine_version=ENGINE)
    assert other.job.artifacts[DISCOVERY_ARTIFACT] == first.job.artifacts[DISCOVERY_ARTIFACT]


def test_provider_shapes_and_provenance() -> None:
    source = MODELS + (
        "from collections.abc import Iterator\n"
        "from sqlalchemy.orm import Session\n"
        "def by_annotation() -> Iterator[Session]:\n    yield unknown_factory()\n"
        "def returns_session():\n    return SessionLocal()\n"
        "def not_a_provider():\n    return 42\n"
    )
    db = discover({"app/models.py": source}).database
    providers = {p.symbol.split(":")[1]: (p.shape.value, p.provenance) for p in db.session_providers}
    assert providers == {
        "by_annotation": ("generator", Provenance.INFERRED),
        "get_db": ("generator", Provenance.EXPLICIT),
        "returns_session": ("function", Provenance.EXPLICIT),
    }


def test_database_discovery_runs_in_an_empty_environment_without_orm_modules(tmp_path: Path) -> None:
    repo = write_tree(tmp_path / "repo", {"app/models.py": MODELS})
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from patchfrog.app.discovery import run_discovery\n"
        "from patchfrog.app.store import LocalRunStore\n"
        f"r = run_discovery(LocalRunStore(Path({str(tmp_path / 'store')!r})), Path({str(repo)!r}), engine_version='0.1.0-beta')\n"
        "heavy = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'sqlalchemy', 'sqlmodel', 'alembic', 'psycopg2', 'asyncpg', 'celery', 'redis', 'fastapi', 'apps', 'anthropic', 'openai'} "
        "or m.startswith(('patchfrog.persistence', 'patchfrog.review', 'patchfrog.config.settings', 'patchfrog.github')))\n"
        "print(json.dumps({'models': len(r.model.database.models), 'heavy': heavy}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"models": 2, "heavy": []}


@pytest.mark.parametrize("annotation", ["Mapped[list['Post']]", "Mapped['list[Post]']", "Mapped[set[Post]]", "Mapped[Optional['Post']]", "Mapped['Post | None']"])
def test_relationship_targets_from_annotations(annotation: str) -> None:
    source = MODELS.replace('posts: Mapped[list["Post"]] = relationship(back_populates="author")', f"posts: {annotation} = relationship()")
    source = "from typing import Optional\n" + source
    user = next(m for m in discover({"app/models.py": source}).database.models if m.name == "User")
    (posts,) = user.relationships
    assert posts.target_model == "model:app.models:Post"
    assert posts.collection is (annotation.startswith(("Mapped[list", "Mapped['list", "Mapped[set"))), annotation


def test_string_values_that_mention_orm_names_are_not_possible_models() -> None:
    """Regression: found on this repository (an enum whose *values* are
    "mapped_column", "relationship_target" ...) during the 2.4 spot check."""

    source = (
        "from enum import StrEnum\nfrom sqlalchemy import Column\n"
        "class Kind(StrEnum):\n    MAPPED_COLUMN = 'mapped_column'\n    TARGET = 'relationship(x)'\n    NAME = '__tablename__'\n"
    )
    assert discover({"kinds.py": source}).database.unresolved == ()
