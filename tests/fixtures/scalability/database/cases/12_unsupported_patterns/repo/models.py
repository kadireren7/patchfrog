from sqlalchemy import Column, Integer, MetaData, Table
from sqlalchemy.orm import as_declarative, registry

mapper_registry = registry()
Base = mapper_registry.generate_base()

metadata = MetaData()
users_table = Table("users", metadata, Column("id", Integer, primary_key=True))


class User:
    pass


mapper_registry.map_imperatively(User, users_table)


@as_declarative()
class LegacyBase:
    id = Column(Integer, primary_key=True)


class Thing(Base):
    __tablename__ = "things"
    id = Column(Integer, primary_key=True)
