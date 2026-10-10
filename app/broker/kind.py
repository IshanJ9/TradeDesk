"""Which kind of account a broker object is: the simulated one, or a real 021 account (live or not yet connected)."""

from typing import Literal

from app.broker.mock import MockBroker

AccountKind = Literal["mock", "021"]


def broker_kind(broker: object) -> AccountKind:
    return "mock" if isinstance(broker, MockBroker) else "021"
