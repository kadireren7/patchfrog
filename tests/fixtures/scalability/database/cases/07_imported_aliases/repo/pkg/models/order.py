import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pkg.base import Base
from pkg.models.customer import Customer


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id = sa.Column(sa.ForeignKey("customers.id"))
    customer = relationship(Customer, back_populates="orders")
