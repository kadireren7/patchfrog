from sqlalchemy.orm import Mapped, mapped_column, relationship

from base import AuditBase, Base


class User(Base):
    __tablename__ = "billing_users"
    id: Mapped[int] = mapped_column(primary_key=True)


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner = relationship("User")
    crm_owner = relationship("crm.models.User")
    audit = relationship("AuditEntry")


class AuditEntry(AuditBase):
    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(primary_key=True)
