from typing import Annotated

from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from app.database import get_db, get_session, get_tx

app = FastAPI()
SessionDep = Annotated[Session, Depends(get_session)]


class Repo:
    def __init__(self, db):
        self.db = db


def get_repo(db: Session = Depends(get_db)):
    return Repo(db)


@app.get("/items")
def list_items(db: Session = Depends(get_db)):
    return []


@app.get("/orders")
def list_orders(session: SessionDep):
    return []


@app.get("/repo")
def via_repo(repo: Repo = Depends(get_repo)):
    return []


@app.post("/tx", dependencies=[Depends(get_tx)])
def transactional():
    return {}


@app.get("/health")
def health():
    return {}
