"""Stands in for a user's 021 session that could not be opened (a login the broker refused, no network at startup, the
server key missing). Every call fails the same safe way, so nothing can be read from or sent to a broker that is not
there. `needs_reconnect` makes the approval services refuse before they claim a card; the Settings screen offers
Reconnect."""

from collections.abc import AsyncIterator, Sequence
from datetime import date

from app.broker.base import BrokerAdapter, BrokerTimeout

MESSAGE = "Your 021 account isn't connected. Open Settings to reconnect it. Nothing was sent."
RECONNECT_MESSAGE = "Your 021 connection needs to be reconnected (Settings, 021 account). Nothing was sent."


class BrokerDisconnected(BrokerTimeout):
    pass


class DisconnectedBroker(BrokerAdapter):
    needs_reconnect = True
    kind = "021"

    def __init__(self, reason: str = MESSAGE):
        self.reason = reason

    def _fail(self, *_a, **_k):
        raise BrokerDisconnected(self.reason)

    get_funds = get_holdings = get_positions = get_orders = get_order = get_quote = get_instrument = _fail
    search_instruments = get_option_expiries = get_option_chain = get_account_locks = _fail
    place_order = modify_order = cancel_order = _fail

    async def find_sent_order(self, *_a, **_k):
        raise BrokerDisconnected(self.reason)

    async def find_option(self, *_a, **_k):
        raise BrokerDisconnected(self.reason)

    async def find_future(self, *_a, **_k):
        raise BrokerDisconnected(self.reason)

    async def subscribe_ticks(self, instrument_keys: Sequence[str]) -> AsyncIterator:  # no prices, ever
        return
        yield  # pragma: no cover  (makes this an async generator)
