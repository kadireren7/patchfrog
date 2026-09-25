"""Initial schema."""


def upgrade(op):
    op.create_table("customers", ["id", "name"])
    op.create_table("invoices", ["id", "customer_id", "gross"])


def downgrade(op):
    op.drop_table("invoices")
    op.drop_table("customers")
