class Account:
    def __init__(self, balance: float) -> None:
        self.balance = balance


def transfer(source: Account, destination: Account, amount: float) -> None:
    """Move `amount` out of `source` and into `destination`."""
    source.balance -= amount
    destination.balance += amount


def refund_customer(merchant: Account, customer: Account, amount: float) -> None:
    """Refund the customer: money should move from the merchant to the customer."""
    transfer(customer, merchant, amount)
