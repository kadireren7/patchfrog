"""Bounded benchmark for application discovery (Milestones 2.3-2.4). A developer
tool, never run by CI and never a gate: it prints single-host measurements
for docs/architecture/DISCOVERY.md (TEST_STRATEGY.md: trends, not claims).

    python scripts/bench_discovery.py [--repo PATH]

Measures, each in a fresh temporary store:
* the synthetic layered FastAPI fixture (routers -> services -> repositories),
  at 300 and 3,000 files;
* the synthetic ORM fixture (models with chained relationships, an engine,
  a session provider, one route per model), at 300 and 3,000 models;
* optionally a real directory (``--repo``), e.g. this repository.
Reports wall time (first run and the idempotent repeat), the peak traced
Python allocation (tracemalloc) and the process peak RSS.
"""

from __future__ import annotations

import argparse
import resource
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from patchfrog.app.discovery import run_discovery
from patchfrog.app.store import LocalRunStore


def layered_fixture(root: Path, resources: int) -> int:
    files = {"app/__init__.py": "", "app/api/__init__.py": "", "app/services/__init__.py": "", "app/repositories/__init__.py": ""}
    includes = []
    for i in range(resources):
        files[f"app/repositories/repo_{i}.py"] = f"class Repo{i}:\n    def all(self):\n        return []\n"
        files[f"app/services/service_{i}.py"] = f"from app.repositories.repo_{i} import Repo{i}\n\ndef get_service_{i}():\n    return Repo{i}()\n"
        files[f"app/api/resource_{i}.py"] = (
            "from fastapi import APIRouter, Depends\n"
            f"from app.services.service_{i} import get_service_{i}\n"
            f"router = APIRouter(prefix='/r{i}')\n"
            f"@router.get('/')\nasync def list_{i}(svc=Depends(get_service_{i})):\n    return svc.all()\n"
            f"@router.post('/')\ndef create_{i}(svc=Depends(get_service_{i})):\n    return None\n"
        )
        includes.append(f"from app.api.resource_{i} import router as r{i}\napp.include_router(r{i}, prefix='/api')\n")
    files["app/main.py"] = "from fastapi import FastAPI\napp = FastAPI()\n" + "".join(includes)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return len(files)


def orm_fixture(root: Path, models: int) -> int:
    """A layered app with ``models`` SQLAlchemy models (chained relationships),
    an engine, a sessionmaker, a session provider and one route per model."""

    files = {
        "app/__init__.py": "",
        "app/models/__init__.py": "",
        "app/api/__init__.py": "",
        "app/db.py": (
            "from sqlalchemy import create_engine\nfrom sqlalchemy.orm import DeclarativeBase, sessionmaker\n"
            "engine = create_engine('postgresql://localhost/bench', pool_size=5)\nSessionLocal = sessionmaker(bind=engine)\n"
            "class Base(DeclarativeBase):\n    pass\n"
            "def get_db():\n    db = SessionLocal()\n    try:\n        yield db\n    finally:\n        db.close()\n"
        ),
    }
    includes = []
    for i in range(models):
        files[f"app/models/m{i}.py"] = (
            "from sqlalchemy import ForeignKey\nfrom sqlalchemy.orm import Mapped, mapped_column, relationship\nfrom app.db import Base\n"
            f"class M{i}(Base):\n    __tablename__ = 't{i}'\n    id: Mapped[int] = mapped_column(primary_key=True)\n    name: Mapped[str]\n"
            + (f"    parent_id: Mapped[int] = mapped_column(ForeignKey('t{i - 1}.id'))\n    parent: Mapped['M{i - 1}'] = relationship(lazy='selectin')\n" if i else "")
        )
        files[f"app/api/r{i}.py"] = (
            "from fastapi import APIRouter, Depends\nfrom app.db import get_db\n"
            f"router = APIRouter(prefix='/m{i}')\n@router.get('/')\ndef list_{i}(db=Depends(get_db)):\n    return []\n"
        )
        includes.append(f"from app.api.r{i} import router as r{i}\napp.include_router(r{i})\n")
    files["app/main.py"] = "from fastapi import FastAPI\napp = FastAPI()\n" + "".join(includes)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return len(files)


def measure(label: str, repo: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LocalRunStore(Path(tmp) / "store")
        tracemalloc.start()
        started = time.perf_counter()
        first = run_discovery(store, repo, engine_version="bench")
        first_s = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        started = time.perf_counter()
        again = run_discovery(store, repo, engine_version="bench")
        repeat_s = time.perf_counter() - started
    model = first.model
    assert model is not None and (again.job.reused or first.job.state.value == "partial")  # PARTIAL is never reused
    print(
        f"{label}: state={first.job.state.value} python_files={model.coverage.python_files} "
        f"scanned={model.coverage.scanned_files} parsed={model.coverage.parsed_files} routes={len(model.routes)} "
        f"unresolved={len(model.unresolved)} models={len(model.database.models)} "
        f"route_sessions={len(model.database.route_sessions)} first={first_s:.2f}s repeat(reused)={repeat_s:.2f}s "
        f"traced_peak={peak / 2**20:.1f} MiB"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path)
    args = parser.parse_args()
    for resources in (100, 1000):
        with tempfile.TemporaryDirectory() as tmp:
            count = layered_fixture(Path(tmp) / "repo", resources)
            measure(f"layered fixture ({count} files)", Path(tmp) / "repo")
    for models in (300, 2000):  # 2,000 models = 4,005 files, under the default 5,000-file limit
        with tempfile.TemporaryDirectory() as tmp:
            count = orm_fixture(Path(tmp) / "repo", models)
            measure(f"ORM fixture ({count} files)", Path(tmp) / "repo")
    if args.repo:
        measure(f"directory {args.repo.resolve().name}", args.repo)
    print(f"process peak RSS: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
