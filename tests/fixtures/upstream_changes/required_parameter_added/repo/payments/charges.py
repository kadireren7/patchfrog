import paylane

client = paylane.Client()


def create_charge(amount: int, currency: str) -> str:
    return client.charges.create(amount=amount, currency=currency).id
