from sqlalchemy import Column, Integer, MetaData, Table, select
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, Mapped, column_property, declared_attr, mapped_column, registry

engine = None


class Base(DeclarativeBase):
    @declared_attr.directive
    def __tablename__(cls) -> str:
        return cls.__name__.lower()


class Account(Base):
    id: Mapped[int] = mapped_column(primary_key=True)
    balance: Mapped[int]
    doubled = column_property(balance * 2)

    @hybrid_property
    def positive(self):
        return self.balance > 0


Dynamic = type("Dynamic", (Base,), {"__tablename__": "dynamic", "id": Column(Integer, primary_key=True)})

mapper_registry = registry()


@mapper_registry.mapped
class Legacy:
    __tablename__ = "legacy"
    id = Column(Integer, primary_key=True)


class Reflected(Base):
    __table__ = Table("reflected", MetaData(), autoload_with=engine)
