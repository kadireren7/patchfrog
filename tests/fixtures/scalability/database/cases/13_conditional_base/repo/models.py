import os

from sqlalchemy import Column, Integer
from sqlalchemy.orm import declarative_base

if os.environ.get("TESTING"):
    Base = declarative_base()
else:
    Base = declarative_base(name="ProdBase")


class Item(Base):
    __tablename__ = "items"
    id = Column(Integer, primary_key=True)
