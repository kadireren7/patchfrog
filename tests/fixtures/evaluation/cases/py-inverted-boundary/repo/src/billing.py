class Account:
    def __init__(self, balance: float) -> None:
        self.balance = balance

    def can_withdraw(self, amount: float) -> bool:
        return amount >= self.balance

    def deposit(self, amount: float) -> None:
        self.balance += amount
