"""Broker boundary. Everything the app knows about a broker goes through this interface.

Two implementations: `MockBroker` (app/broker/mock.py) and, after kickoff, the real 021 adapter.

Safety shape
- Write methods (`place_order`, `modify_order`, `cancel_order`) take an *approved*
  `PendingOrder`. An adapter refuses anything whose state is not APPROVED, as a second
  lock behind the executor.
- The LLM module only ever receives `broker.read_only()`: an object that has no write
  methods at all. (Python cannot truly hide attributes; the point is that no write
  method is part of the surface the LLM tools are built against.)
- Errors are typed. `BrokerTimeout` means the outcome is UNKNOWN: the caller must
  reconcile via `get_order(client_order_id)` / `get_orders()` and never retry blindly.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from datetime import date
from typing import Protocol

from app.schemas import (
    AccountLocks,
    Funds,
    Holding,
    Instrument,
    OptionChain,
    Order,
    PendingOrder,
    PendingState,
    Position,
    Quote,
    RejectionReason,
    Tick,
)

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class BrokerError(Exception):
    pass


class BrokerTimeout(BrokerError):
    """The request may or may not have reached the broker. Outcome unknown."""


class BrokerRejected(BrokerError):
    """The broker refused the request. `order` is the rejected record if one exists."""

    def __init__(self, reason: RejectionReason, message: str = "", order: Order | None = None):
        super().__init__(f"{reason.value}: {message}" if message else reason.value)
        self.reason = reason
        self.message = message
        self.order = order


class OrderNotApproved(BrokerError):
    """A write was attempted with an order that is not in APPROVED state."""


# --------------------------------------------------------------------------- #
# Read-only surface
# --------------------------------------------------------------------------- #


class ReadOnlyBroker(Protocol):
    """What the LLM tools may use. No way to place, modify or cancel."""

    async def get_funds(self) -> Funds: ...

    async def get_holdings(self) -> list[Holding]: ...

    async def get_positions(self) -> list[Position]: ...

    async def get_orders(self) -> list[Order]: ...

    async def get_order(self, client_order_id: str) -> Order | None: ...

    async def get_quote(self, instrument_key: str) -> Quote: ...

    async def get_instrument(self, instrument_key: str) -> Instrument | None: ...

    async def search_instruments(self, query: str, limit: int = 10) -> list[Instrument]: ...

    async def get_option_expiries(self, underlying: str) -> list[date]: ...

    async def get_option_chain(self, underlying: str, expiry: date, window: int = 5) -> OptionChain: ...

    async def get_account_locks(self) -> AccountLocks: ...

    def subscribe_ticks(self, instrument_keys: Sequence[str]) -> AsyncIterator[Tick]: ...


class ReadOnlyView:
    """Wraps a broker and exposes only the read methods."""

    def __init__(self, broker: "BrokerAdapter"):
        self._broker = broker

    async def get_funds(self) -> Funds:
        return await self._broker.get_funds()

    async def get_holdings(self) -> list[Holding]:
        return await self._broker.get_holdings()

    async def get_positions(self) -> list[Position]:
        return await self._broker.get_positions()

    async def get_orders(self) -> list[Order]:
        return await self._broker.get_orders()

    async def get_order(self, client_order_id: str) -> Order | None:
        return await self._broker.get_order(client_order_id)

    async def get_quote(self, instrument_key: str) -> Quote:
        return await self._broker.get_quote(instrument_key)

    async def get_instrument(self, instrument_key: str) -> Instrument | None:
        return await self._broker.get_instrument(instrument_key)

    async def search_instruments(self, query: str, limit: int = 10) -> list[Instrument]:
        return await self._broker.search_instruments(query, limit)

    async def get_option_expiries(self, underlying: str) -> list[date]:
        return await self._broker.get_option_expiries(underlying)

    async def get_option_chain(self, underlying: str, expiry: date, window: int = 5) -> OptionChain:
        return await self._broker.get_option_chain(underlying, expiry, window)

    async def get_account_locks(self) -> AccountLocks:
        return await self._broker.get_account_locks()

    def subscribe_ticks(self, instrument_keys: Sequence[str]) -> AsyncIterator[Tick]:
        return self._broker.subscribe_ticks(instrument_keys)


# --------------------------------------------------------------------------- #
# Full adapter
# --------------------------------------------------------------------------- #


class BrokerAdapter(ABC):
    """Full broker interface. Only the executor should hold one of these."""

    # ---- reads ----

    @abstractmethod
    async def get_funds(self) -> Funds: ...

    @abstractmethod
    async def get_holdings(self) -> list[Holding]: ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def get_orders(self) -> list[Order]:
        """Order book, newest first."""

    @abstractmethod
    async def get_order(self, client_order_id: str) -> Order | None:
        """The most recent order carrying this client order id, or None. Used to reconcile."""

    @abstractmethod
    async def get_quote(self, instrument_key: str) -> Quote: ...

    @abstractmethod
    async def get_instrument(self, instrument_key: str) -> Instrument | None: ...

    @abstractmethod
    async def search_instruments(self, query: str, limit: int = 10) -> list[Instrument]: ...

    @abstractmethod
    async def get_option_expiries(self, underlying: str) -> list[date]:
        """Real expiry dates from the instrument master. Never assume a weekday."""

    @abstractmethod
    async def get_option_chain(self, underlying: str, expiry: date, window: int = 5) -> OptionChain:
        """Strikes within `window` steps either side of the at-the-money strike."""

    @abstractmethod
    async def get_account_locks(self) -> AccountLocks: ...

    @abstractmethod
    def subscribe_ticks(self, instrument_keys: Sequence[str]) -> AsyncIterator[Tick]: ...

    # ---- writes (approved PendingOrder only) ----

    @abstractmethod
    async def place_order(self, order: PendingOrder) -> Order: ...

    @abstractmethod
    async def modify_order(self, order: PendingOrder) -> Order: ...

    @abstractmethod
    async def cancel_order(self, order: PendingOrder) -> Order: ...

    # ---- shared helpers ----

    def read_only(self) -> ReadOnlyBroker:
        return ReadOnlyView(self)

    @staticmethod
    def require_approved(order: PendingOrder) -> None:
        if order.state is not PendingState.APPROVED:
            raise OrderNotApproved(f"order {order.id} is {order.state.value}, not APPROVED")
