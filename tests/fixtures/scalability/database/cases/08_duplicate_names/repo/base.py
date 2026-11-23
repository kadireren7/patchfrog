from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class AuditBase(DeclarativeBase):
    pass
