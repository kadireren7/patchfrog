from external_orm.base import ExternalBase
from fastapi import Depends, FastAPI
from sqlalchemy import Column, ForeignKey, Integer
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from vendor_auth.db import get_session

from config import load_settings


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "widgets"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner = relationship("Owner")
    parts = relationship(lambda: Part)


class Sprocket(Base):
    __tablename__ = "sprockets"
    id = Column(Integer, ForeignKey("widgets.id"), primary_key=IS_PRIMARY)


class Gadget(ExternalBase):
    __tablename__ = "gadgets"
    id: Mapped[int] = mapped_column(primary_key=True)


app = FastAPI()


@app.get("/widgets")
def widgets(session=Depends(get_session)):
    return load_settings()
