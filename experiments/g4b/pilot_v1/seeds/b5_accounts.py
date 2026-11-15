import threading


class InsufficientFunds(Exception):
    pass


class Account:
    def __init__(self, name: str, cents: int = 0) -> None:
        self.name = name
        self.cents = cents
        self.lock = threading.Lock()


def deposit(acct: Account, cents: int) -> None:
    if cents <= 0:
        raise ValueError("deposit must be positive")
    with acct.lock:
        acct.cents += cents


def transfer(src: Account, dst: Account, cents: int) -> None:
    """Move money between two accounts atomically."""
    if cents <= 0:
        raise ValueError("transfer must be positive")
    if src is dst:
        return
    with src.lock:
        with dst.lock:
            if src.cents < cents:
                raise InsufficientFunds(src.name)
            src.cents -= cents
            dst.cents += cents


def balance(acct: Account) -> int:
    with acct.lock:
        return acct.cents
