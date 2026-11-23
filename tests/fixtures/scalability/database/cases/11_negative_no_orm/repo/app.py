"""Negative control: nothing here is SQLAlchemy or SQLModel."""

from dataclasses import dataclass, field

import requests
from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field


class Session:
    """A local class that happens to be called Session."""

    def close(self):
        pass


class UserOut(BaseModel):
    id: int = Field(gt=0)
    name: str


@dataclass
class Graph:
    relationship: list[str] = field(default_factory=list)


def get_http():
    with requests.Session() as http:
        yield http


def get_local_session():
    session = Session()
    yield session


app = FastAPI()


@app.get("/users")
def users(http=Depends(get_http), s=Depends(get_local_session)):
    return []
