import paylane

client = paylane.Client()


def refund(charge_id: str) -> str:
    return client.refunds.create(charge=charge_id).id
