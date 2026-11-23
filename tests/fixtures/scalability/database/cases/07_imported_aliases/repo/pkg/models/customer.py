import sqlalchemy as sa
from sqlalchemy import orm

from pkg.base import Base as ModelBase


class Customer(ModelBase):
    __tablename__ = "customers"
    id: orm.Mapped[int] = orm.mapped_column(primary_key=True)
    orders = orm.relationship("pkg.models.order.Order", back_populates="customer", lazy="dynamic")
    email = sa.Column(sa.String, nullable=False)
