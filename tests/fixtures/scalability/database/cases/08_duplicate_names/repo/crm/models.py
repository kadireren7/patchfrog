from sqlalchemy.orm import Mapped, mapped_column

from base import Base


class User(Base):
    __tablename__ = "crm_users"
    id: Mapped[int] = mapped_column(primary_key=True)
