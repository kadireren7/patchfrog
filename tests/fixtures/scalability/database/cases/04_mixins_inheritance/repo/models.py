from sqlalchemy import ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AuditMixin:
    created_by: Mapped[str] = mapped_column(default="system")


class Identified(Base):
    __abstract__ = True
    id: Mapped[int] = mapped_column(primary_key=True)


class Employee(AuditMixin, Identified):
    __tablename__ = "employees"
    kind: Mapped[str]
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "employee"}


class Manager(Employee):
    __tablename__ = "managers"
    id: Mapped[int] = mapped_column(ForeignKey("employees.id"), primary_key=True)
    budget: Mapped[int]
    __mapper_args__ = {"polymorphic_identity": "manager"}


class Engineer(Employee):
    language: Mapped[str] = mapped_column(nullable=True)
    __mapper_args__ = {"polymorphic_identity": "engineer"}
