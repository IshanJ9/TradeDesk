"""Shared data contract for TradeDesk-AI. Pydantic is the single source of truth.

Conventions
- Money is an integer in paise. Quantities are integer share units.
- Every model is frozen and rejects unknown fields (extra="forbid"), so a field the
  LLM invents (e.g. "approved": true) is an error, never silently ignored.
- Timestamps are timezone-aware.
- The LLM may only emit `OrderIntent`. Everything else is built by code.
- `PendingOrder.order_hash` is computed, never stored: it cannot be forged or go stale.
"""

import hashlib
import json
import unicodedata
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

# --------------------------------------------------------------------------- #
# Money and text helpers
# --------------------------------------------------------------------------- #

Paise = Annotated[int, Field(ge=0)]  # non-negative amount
PricePaise = Annotated[int, Field(gt=0)]  # a price per share
Quantity = Annotated[int, Field(gt=0)]


def paise(rupees: int | float | str | Decimal) -> int:
    """Rupees -> integer paise (half-up). paise("1450") == 145000."""
    return int((Decimal(str(rupees)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _indian_group(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts: list[str] = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def fmt_rupees(amount_paise: int) -> str:
    """Integer paise -> display string with Indian digit grouping, e.g. Rs 1,00,000.50."""
    sign = "-" if amount_paise < 0 else ""
    whole, frac = divmod(abs(amount_paise), 100)
    return f"{sign}₹{_indian_group(whole)}.{frac:02d}"


def sanitize_text(value: str, max_len: int = 120) -> str:
    """Strip control characters, collapse whitespace, cap length.

    Used on every free-text field that comes from outside (broker names, user text).
    It does not try to detect injection; it only limits what text can carry.
    """
    cleaned = "".join(
        " " if ch in "\r\n\t" else ch
        for ch in value
        if ch in "\r\n\t" or not unicodedata.category(ch).startswith("C")
    )
    return " ".join(cleaned.split())[:max_len]


def _sha256(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Model(BaseModel):
    # The server always sends every field, so response schemas mark defaulted fields as
    # required. That keeps the generated TypeScript types exact (no spurious `field?:`).
    model_config = ConfigDict(frozen=True, extra="forbid", json_schema_serialization_defaults_required=True)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP_LIMIT = "STOP_LIMIT"  # sleeps until the price reaches `trigger_price`, then becomes a limit order


class Product(str, Enum):
    CNC = "CNC"  # delivery
    MIS = "MIS"  # intraday
    NRML = "NRML"  # options carried overnight (021: NRML is the F&O product; CNC is equity only)


class Validity(str, Enum):
    DAY = "DAY"
    IOC = "IOC"  # 021 offers only these two


class OrderAction(str, Enum):
    PLACE = "PLACE"
    MODIFY = "MODIFY"
    CANCEL = "CANCEL"


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"


class OrderStatus(str, Enum):
    PENDING = "PENDING"  # accepted by us, not yet open at the exchange
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"  # timeout: outcome not known -> reconcile, never retry blindly


class RejectionReason(str, Enum):
    # 021 broker/exchange reasons
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    PRICE_BAND = "PRICE_BAND"
    INVALID_PRICE = "INVALID_PRICE"
    INVALID_QUANTITY = "INVALID_QUANTITY"
    MARKET_CLOSED = "MARKET_CLOSED"
    SUSPENDED = "SUSPENDED"
    KYC_DORMANT = "KYC_DORMANT"
    RISK_CHECK = "RISK_CHECK"
    OI_LIMIT = "OI_LIMIT"
    # raised by our own safety layer before anything is sent
    QUANTITY_LIMIT = "QUANTITY_LIMIT"
    VALUE_LIMIT = "VALUE_LIMIT"
    SEGMENT_NOT_ALLOWED = "SEGMENT_NOT_ALLOWED"
    ANCHOR_ACTIVE = "ANCHOR_ACTIVE"
    CO_APPROVAL_REQUIRED = "CO_APPROVAL_REQUIRED"
    OTHER = "OTHER"


class PendingState(str, Enum):
    PENDING = "PENDING"
    AWAITING_CO_APPROVAL = "AWAITING_CO_APPROVAL"  # the trader approved; the Co-Captain must too (app/cocaptain)
    APPROVED = "APPROVED"
    SENT = "SENT"
    REQUOTE_REQUIRED = "REQUOTE_REQUIRED"  # price moved; a fresh PendingOrder replaces this
    EXPIRED = "EXPIRED"
    VOID = "VOID"  # hash mismatch or superseded
    REJECTED = "REJECTED"  # trader said no


class PlanState(str, Enum):
    PENDING = "PENDING"
    AWAITING_CO_APPROVAL = "AWAITING_CO_APPROVAL"  # the trader approved the whole plan; their Co-Captain must too
    REQUOTE_REQUIRED = "REQUOTE_REQUIRED"  # prices moved; a fresh plan replaces this one
    APPROVED = "APPROVED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    HALTED = "HALTED"
    EXPIRED = "EXPIRED"
    VOID = "VOID"
    REJECTED = "REJECTED"


class QuantityBasis(str, Enum):
    FIXED = "FIXED"
    FROM_PROCEEDS = "FROM_PROCEEDS"  # sized from an earlier leg's actual proceeds


class LegFailurePolicy(str, Enum):
    HALT = "HALT"  # a rejected leg stops the remaining legs
    CONTINUE = "CONTINUE"


class LegStatus(str, Enum):
    NOT_SENT = "NOT_SENT"
    SKIPPED = "SKIPPED"  # not sent because an earlier leg failed
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class RuleKind(str, Enum):
    ALERT = "ALERT"
    TRIGGER_ORDER = "TRIGGER_ORDER"


class RuleStatus(str, Enum):
    ACTIVE = "ACTIVE"
    FIRED = "FIRED"
    CANCELLED = "CANCELLED"


class Comparator(str, Enum):
    BELOW = "BELOW"
    ABOVE = "ABOVE"


class RuleBasis(str, Enum):
    ABSOLUTE = "ABSOLUTE"  # a fixed price, e.g. 3800
    AVG_BUY = "AVG_BUY"  # % from the trader's average buy price
    PREV_CLOSE = "PREV_CLOSE"
    AT_CREATION = "AT_CREATION"  # % from the price when the rule was made


class AuditKind(str, Enum):
    COCAPTAIN = "COCAPTAIN"
    VOICE_TRANSCRIBED = "VOICE_TRANSCRIBED"  # voice-live: metadata only, no transcript
    USER_MESSAGE = "USER_MESSAGE"
    LLM_INTENT = "LLM_INTENT"
    RESOLUTION = "RESOLUTION"
    PENDING_CREATED = "PENDING_CREATED"
    APPROVAL = "APPROVAL"
    APPROVAL_REFUSED = "APPROVAL_REFUSED"
    BROKER_REQUEST = "BROKER_REQUEST"
    BROKER_RESPONSE = "BROKER_RESPONSE"
    BROKER_TIMEOUT = "BROKER_TIMEOUT"
    RECONCILE = "RECONCILE"
    RULE_CREATED = "RULE_CREATED"
    RULE_FIRED = "RULE_FIRED"
    RULE_CANCELLED = "RULE_CANCELLED"
    PLAN_STARTED = "PLAN_STARTED"
    PLAN_LEG_RESULT = "PLAN_LEG_RESULT"
    INJECTION_BLOCKED = "INJECTION_BLOCKED"
    LIMIT_BLOCKED = "LIMIT_BLOCKED"
    LOCK_BLOCKED = "LOCK_BLOCKED"
    CHAOS = "CHAOS"
    BROKER_LINK = "BROKER_LINK"  # a 021 account was linked, unlinked or reconnected: never any credentials


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #


class Instrument(Model):
    symbol: str = Field(pattern=r"^[A-Z0-9&\-_.]{1,40}$")
    exchange: Exchange
    series: str = "EQ"
    isin: str | None = None
    name: str = ""
    tick_size: PricePaise = 5
    price_band_low: PricePaise | None = None
    price_band_high: PricePaise | None = None
    suspended: bool = False
    # Option-contract fields. Used by the option chain only; orders never use them.
    underlying: str | None = None
    lot_size: Quantity | None = None
    expiry: date | None = None
    strike: PricePaise | None = None
    option_type: OptionType | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _clean_name(cls, v: Any) -> Any:
        return sanitize_text(v) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _contract_fields_together(self) -> "Instrument":
        if self.series == "FUT":  # a future has an expiry and a lot but no strike or call/put
            if self.strike is not None or self.option_type is not None:
                raise ValueError("a future has no strike or option_type")
            if self.underlying is None or self.expiry is None or self.lot_size is None:
                raise ValueError("a future needs underlying, expiry and lot_size")
            return self
        parts = [self.option_type, self.expiry, self.strike, self.lot_size]
        if any(p is not None for p in parts) and any(p is None for p in parts):
            raise ValueError("option_type, expiry, strike and lot_size must be set together")
        return self

    @property
    def key(self) -> str:
        return f"{self.exchange.value}:{self.symbol}"

    @property
    def is_option(self) -> bool:
        return self.option_type is not None

    @property
    def is_future(self) -> bool:
        return self.series == "FUT"

    @property
    def is_derivative(self) -> bool:
        return self.is_option or self.is_future

    @property
    def label(self) -> str:
        """What a trader calls it: 'Infosys Ltd', 'NIFTY 24,500 CE (13 Oct 2026)', or 'NIFTY FUT (27 Oct 2026)'."""
        if self.is_future:
            return f"{self.underlying} FUT ({self.expiry:%d %b %Y})"
        if not self.is_option:
            return self.name or self.symbol
        strike = f"{self.strike // 100:,}" if self.strike % 100 == 0 else f"{self.strike / 100:,.2f}"
        return f"{self.underlying} {strike} {self.option_type.value} ({self.expiry:%d %b %Y})"


class Quote(Model):
    instrument_key: str
    ltp: PricePaise
    prev_close: PricePaise
    bid: PricePaise | None = None
    ask: PricePaise | None = None
    day_high: PricePaise | None = None
    day_low: PricePaise | None = None
    ts: AwareDatetime

    @model_validator(mode="after")
    def _range(self) -> "Quote":
        if self.day_high is not None and self.day_low is not None and self.day_low > self.day_high:
            raise ValueError("day_low cannot be above day_high")
        return self


class Tick(Model):
    instrument_key: str
    ltp: PricePaise
    seq: Annotated[int, Field(ge=0)]  # per-instrument sequence, lets consumers drop dupes
    ts: AwareDatetime


class OptionQuote(Model):
    instrument_key: str
    ltp: Paise
    # None = not known. 021's sandbox fills these two with random numbers, so the live adapter leaves them unset.
    oi: Annotated[int, Field(ge=0)] | None = None
    volume: Annotated[int, Field(ge=0)] | None = None


class OptionChainRow(Model):
    strike: PricePaise
    call: OptionQuote | None = None
    put: OptionQuote | None = None


class OptionChain(Model):
    underlying: str
    spot: PricePaise
    expiry: date
    rows: list[OptionChainRow]


# --------------------------------------------------------------------------- #
# Account
# --------------------------------------------------------------------------- #


class _Valued(Model):
    """Shared valuation maths. All numbers are computed here, never by the LLM."""

    instrument: Instrument
    quantity: int
    avg_price: PricePaise
    ltp: PricePaise
    prev_close: PricePaise

    @computed_field
    @property
    def invested(self) -> int:
        return self.avg_price * abs(self.quantity)

    @computed_field
    @property
    def current_value(self) -> int:
        return self.ltp * abs(self.quantity)

    @computed_field
    @property
    def pnl(self) -> int:
        """Unrealised P&L in paise vs average price (negative quantity = short)."""
        return (self.ltp - self.avg_price) * self.quantity

    @computed_field
    @property
    def pnl_pct(self) -> float:
        """P&L as a percentage of the amount invested, rounded to 2 decimals."""
        return round(self.pnl * 100 / self.invested, 2)

    @computed_field
    @property
    def day_pnl(self) -> int:
        """Today's P&L in paise vs previous close."""
        return (self.ltp - self.prev_close) * self.quantity


class Holding(_Valued):
    quantity: Quantity


class Position(_Valued):
    product: Product = Product.MIS

    @field_validator("quantity")
    @classmethod
    def _non_zero(cls, v: int) -> int:
        if v == 0:
            raise ValueError("a position cannot have zero quantity")
        return v


class Funds(Model):
    available_cash: int
    used_margin: Paise = 0

    @computed_field
    @property
    def total(self) -> int:
        return self.available_cash + self.used_margin


class AccountLocks(Model):
    """021's discipline features as the copilot must respect them."""

    anchor_active: bool = False
    anchor_message: str | None = None
    anchor_until: AwareDatetime | None = None
    buffett_mode: bool = False
    co_captain_locked: bool = False
    co_captain_message: str | None = None
    co_approval_limit: Paise | None = None  # orders above this need a Co-Captain too

    @field_validator("anchor_message", "co_captain_message", mode="before")
    @classmethod
    def _clean_message(cls, v: Any) -> Any:
        return sanitize_text(v, 300) if isinstance(v, str) else v


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #


class Charges(Model):
    """021's charge breakdown for one order, in paise.

    `clearing` is kept for the plan's 8-part list; 021's pricing page lists no clearing
    charge for equity, so it is 0. `dp_charge` is the per-ISIN depository charge 021
    levies on delivery sells.
    """

    brokerage: Paise = 0
    stt: Paise = 0
    exchange_txn: Paise = 0
    sebi_fee: Paise = 0
    stamp_duty: Paise = 0
    gst: Paise = 0
    clearing: Paise = 0
    ipft: Paise = 0  # investor protection fund
    dp_charge: Paise = 0  # delivery sells only
    break_even_price: Paise = 0  # per share, round trip, includes charges

    @computed_field
    @property
    def total(self) -> int:
        return (
            self.brokerage
            + self.stt
            + self.exchange_txn
            + self.sebi_fee
            + self.stamp_duty
            + self.gst
            + self.clearing
            + self.ipft
            + self.dp_charge
        )


class OptionRef(Model):
    """Which option contract the trader named. Code finds the exact contract; nothing here is trusted as a price."""

    underlying: str = Field(min_length=1, max_length=20)  # "NIFTY", "BANKNIFTY", "RELIANCE"
    strike: PricePaise
    option_type: OptionType
    expiry: date | None = None  # None: the nearest expiry, which the card then states

    @field_validator("underlying", mode="before")
    @classmethod
    def _clean_underlying(cls, v: Any) -> Any:
        return sanitize_text(v, 20).upper().replace(" ", "") if isinstance(v, str) else v


class FutureRef(Model):
    """Which futures contract the trader named. Code finds the exact contract."""

    underlying: str = Field(min_length=1, max_length=20)  # "NIFTY", "BANKNIFTY", "RELIANCE"
    expiry: date | None = None  # None: the nearest expiry, which the card then states

    @field_validator("underlying", mode="before")
    @classmethod
    def _clean_underlying(cls, v: Any) -> Any:
        return sanitize_text(v, 20).upper().replace(" ", "") if isinstance(v, str) else v


class OrderIntent(Model):
    """The only order-shaped object the LLM may emit. Not an order: code resolves and validates it."""

    action: OrderAction
    instrument_ref: str | None = Field(default=None, max_length=60)
    option: OptionRef | None = None  # an option contract instead of a stock (buy to open, or sell what is held)
    future: FutureRef | None = None  # a futures contract instead of a stock
    side: Side | None = None
    quantity: Quantity | None = None
    lots: Quantity | None = None  # options and futures only: whole lots; code multiplies by the contract's lot size
    amount_paise: PricePaise | None = None  # "worth Rs 10k"; code converts to whole shares
    fraction_of_holding: Annotated[float, Field(gt=0, le=1)] | None = None  # SELL: "half my TCS"; code does the sum
    order_type: OrderType | None = None
    limit_price: PricePaise | None = None
    trigger_price: PricePaise | None = None  # STOP_LIMIT: the price that wakes the order up
    product: Product = Product.CNC
    validity: Validity = Validity.DAY
    target_order_id: str | None = None  # for MODIFY / CANCEL
    # Set by code from where the message came from, never by the model: high-risk orders are not started by voice.
    via_voice: bool = False

    @field_validator("instrument_ref", mode="before")
    @classmethod
    def _clean_ref(cls, v: Any) -> Any:
        return sanitize_text(v, 60) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _check(self) -> "OrderIntent":
        if self.action is OrderAction.PLACE:
            if sum(x is not None for x in (self.instrument_ref or None, self.option, self.future)) != 1:
                raise ValueError("PLACE needs exactly one of instrument_ref, option or future")
            if self.side is None:
                raise ValueError("PLACE needs side")
            if self.order_type is None:
                raise ValueError("PLACE needs order_type")
            if sum(x is not None for x in (self.quantity, self.lots, self.amount_paise, self.fraction_of_holding)) != 1:
                raise ValueError("give exactly one of quantity, lots, amount_paise or fraction_of_holding")
            if self.lots is not None and self.option is None and self.future is None:
                raise ValueError("lots only applies to an option or a future")
            if (self.option is not None or self.future is not None) and self.amount_paise is not None:
                raise ValueError("an option or future is sized in lots, not a rupee amount")
            if self.fraction_of_holding is not None and self.side is not Side.SELL:
                raise ValueError("fraction_of_holding only applies to a SELL")
            if self.order_type is OrderType.LIMIT and self.limit_price is None:
                raise ValueError("LIMIT order needs limit_price")
            if self.order_type is OrderType.MARKET and (self.limit_price is not None or self.trigger_price is not None):
                raise ValueError("MARKET order cannot have limit_price or trigger_price")
            if self.order_type is OrderType.LIMIT and self.trigger_price is not None:
                raise ValueError("LIMIT order cannot have trigger_price")
            if self.order_type is OrderType.STOP_LIMIT and self.trigger_price is None:
                raise ValueError("STOP_LIMIT order needs trigger_price")
            if self.target_order_id is not None:
                raise ValueError("PLACE cannot have target_order_id")
        elif self.action is OrderAction.MODIFY:
            if not self.target_order_id:
                raise ValueError("MODIFY needs target_order_id")
            if self.quantity is None and self.limit_price is None and self.trigger_price is None:
                raise ValueError("MODIFY needs a new quantity, limit_price or trigger_price")
            if self.amount_paise is not None or self.fraction_of_holding is not None or self.lots is not None:
                raise ValueError("MODIFY cannot use amount_paise, lots or fraction_of_holding")
        else:  # CANCEL
            if not self.target_order_id:
                raise ValueError("CANCEL needs target_order_id")
            if any(x is not None for x in (self.quantity, self.amount_paise, self.fraction_of_holding, self.limit_price, self.trigger_price)):
                raise ValueError("CANCEL takes only target_order_id")
        return self


class ResolutionResult(Model):
    """Outcome of turning a user's instrument words into one exact instrument."""

    status: Literal["resolved", "ambiguous", "not_found"]
    query: str
    instrument: Instrument | None = None
    candidates: list[Instrument] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> "ResolutionResult":
        if self.status == "resolved" and (self.instrument is None or self.candidates):
            raise ValueError("resolved needs exactly one instrument and no candidates")
        if self.status == "ambiguous" and (self.instrument is not None or len(self.candidates) < 2):
            raise ValueError("ambiguous needs two or more candidates and no instrument")
        if self.status == "not_found" and (self.instrument is not None or self.candidates):
            raise ValueError("not_found carries no instrument or candidates")
        return self


_PENDING_TRANSITIONS: dict[PendingState, frozenset[PendingState]] = {
    PendingState.AWAITING_CO_APPROVAL: frozenset(
        {
            PendingState.APPROVED,
            PendingState.REQUOTE_REQUIRED,
            PendingState.EXPIRED,
            PendingState.VOID,
            PendingState.REJECTED,
        }
    ),
    PendingState.PENDING: frozenset(
        {
            PendingState.AWAITING_CO_APPROVAL,
            PendingState.APPROVED,
            PendingState.REQUOTE_REQUIRED,
            PendingState.EXPIRED,
            PendingState.VOID,
            PendingState.REJECTED,
        }
    ),
    PendingState.APPROVED: frozenset(
        {PendingState.SENT, PendingState.REQUOTE_REQUIRED, PendingState.EXPIRED, PendingState.VOID}
    ),
}


class InvalidTransition(ValueError):
    pass


class PendingOrder(Model):
    """An exact order awaiting the trader's click. Built by code only."""

    id: str
    action: OrderAction
    instrument: Instrument
    side: Side | None = None
    quantity: Quantity | None = None
    order_type: OrderType | None = None
    limit_price: PricePaise | None = None
    protection_price: PricePaise | None = None  # bound on a MARKET order (protected limit)
    trigger_price: PricePaise | None = None  # STOP_LIMIT only
    product: Product = Product.CNC
    validity: Validity = Validity.DAY
    target_order_id: str | None = None
    # Our own idempotency key. The broker never sees it (021 has no such field); it keys our
    # write-ahead log so the same order cannot be sent twice from this app.
    client_order_id: str
    charges: Charges = Field(default_factory=Charges)
    est_total: int = 0  # buy: cost + charges; sell: proceeds - charges (paise)
    ref_ltp: PricePaise  # price the card was shown at; drift is measured from here
    created_at: AwareDatetime
    expires_at: AwareDatetime
    state: PendingState = PendingState.PENDING
    plan_id: str | None = None
    rule_id: str | None = None
    warnings: list[str] = Field(default_factory=list)
    # Set by code for orders whose loss can exceed what the trader puts in (futures, a sold option not
    # held): Approve is refused unless the request carries the typed acknowledgment (see ApprovalService).
    risk_ack_required: bool = False
    # Set while the card waits for the Co-Captain (app/cocaptain): who, and why a second approval is needed.
    co_captain: str | None = None  # the reviewer's user id (logic compares it)
    co_captain_name: str | None = None  # ...and the name to show
    co_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "PendingOrder":
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        if self.action is OrderAction.PLACE:
            if self.side is None or self.quantity is None or self.order_type is None:
                raise ValueError("PLACE needs side, quantity and order_type")
            if self.order_type is OrderType.LIMIT and self.limit_price is None:
                raise ValueError("LIMIT order needs limit_price")
            if self.order_type is OrderType.MARKET and self.protection_price is None:
                raise ValueError("MARKET order needs protection_price")
            if self.order_type is OrderType.STOP_LIMIT:
                if self.trigger_price is None or self.limit_price is None:
                    raise ValueError("STOP_LIMIT order needs trigger_price and limit_price")
                # 021: a buy stop's price must be at or above its trigger, a sell stop's at or below.
                if self.side is Side.BUY and self.limit_price < self.trigger_price:
                    raise ValueError("a buy STOP_LIMIT needs limit_price at or above trigger_price")
                if self.side is Side.SELL and self.limit_price > self.trigger_price:
                    raise ValueError("a sell STOP_LIMIT needs limit_price at or below trigger_price")
            elif self.trigger_price is not None:
                raise ValueError("only a STOP_LIMIT order has a trigger_price")
        elif not self.target_order_id:
            raise ValueError(f"{self.action.value} needs target_order_id")
        return self

    @computed_field
    @property
    def order_hash(self) -> str:
        """Hash of the exact order. Excludes LTP, expiry and state on purpose.

        Price drift is handled by the drift check and re-quote; the hash only binds what
        the trader approved: what, how much, at what price, under which idempotency key.
        """
        return _sha256(
            {
                "action": self.action.value,
                "instrument": self.instrument.key,
                "side": self.side.value if self.side else None,
                "quantity": self.quantity,
                "order_type": self.order_type.value if self.order_type else None,
                "limit_price": self.limit_price,
                "protection_price": self.protection_price,
                "trigger_price": self.trigger_price,
                "product": self.product.value,
                "validity": self.validity.value,
                "target_order_id": self.target_order_id,
                "client_order_id": self.client_order_id,
            }
        )

    def is_expired(self, now: AwareDatetime) -> bool:
        return now >= self.expires_at

    @property
    def is_terminal(self) -> bool:
        return self.state not in _PENDING_TRANSITIONS

    def transition(self, new_state: PendingState) -> "PendingOrder":
        if new_state not in _PENDING_TRANSITIONS.get(self.state, frozenset()):
            raise InvalidTransition(f"{self.state.value} -> {new_state.value} is not allowed")
        return self.model_copy(update={"state": new_state})


class Order(Model):
    """Broker-side order state, as reported by the broker."""

    order_id: str
    instrument: Instrument
    side: Side
    quantity: Quantity
    filled_quantity: Annotated[int, Field(ge=0)] = 0
    avg_fill_price: PricePaise | None = None
    order_type: OrderType
    limit_price: PricePaise | None = None
    trigger_price: PricePaise | None = None
    product: Product = Product.CNC
    validity: Validity = Validity.DAY
    status: OrderStatus
    rejection_reason: RejectionReason | None = None
    rejection_message: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @field_validator("rejection_message", mode="before")
    @classmethod
    def _clean_msg(cls, v: Any) -> Any:
        return sanitize_text(v, 300) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _check(self) -> "Order":
        if self.filled_quantity > self.quantity:
            raise ValueError("filled_quantity cannot exceed quantity")
        if self.status is OrderStatus.REJECTED and self.rejection_reason is None:
            raise ValueError("REJECTED needs a rejection_reason")
        return self

    @computed_field
    @property
    def pending_quantity(self) -> int:
        active = {OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIAL}
        return self.quantity - self.filled_quantity if self.status in active else 0


class SentOrderSpec(Model):
    """What an order looked like on the wire. 021 has no client order id, so after a timeout this is the
    only way to ask 'did my order get there?': find an order in the book that looks exactly like this."""

    instrument_key: str
    side: Side
    quantity: Quantity
    price: PricePaise  # the limit price actually sent (a protected market order is a limit order on the wire)
    trigger_price: PricePaise | None = None
    product: Product
    validity: Validity
    sent_after: AwareDatetime  # the order cannot have been placed before we started sending it

    @classmethod
    def from_pending(cls, p: "PendingOrder", sent_after: Any) -> "SentOrderSpec":
        return cls(
            instrument_key=p.instrument.key,
            side=p.side,
            quantity=p.quantity,
            price=p.limit_price or p.protection_price,
            trigger_price=p.trigger_price,
            product=p.product,
            validity=p.validity,
            sent_after=sent_after,
        )


class MatchKind(str, Enum):
    FOUND = "FOUND"  # exactly one order in the book looks like it
    NONE = "NONE"  # the book was read and nothing looks like it
    AMBIGUOUS = "AMBIGUOUS"  # several look alike: we cannot tell which (if any) is ours


class MatchResult(Model):
    kind: MatchKind
    order: Order | None = None


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #


class PlanLeg(Model):
    index: Annotated[int, Field(ge=0)]
    order: PendingOrder
    quantity_basis: QuantityBasis = QuantityBasis.FIXED
    # FROM_PROCEEDS: leg sized from the actual proceeds of an earlier sell,
    # within an approved hard cap. `order.quantity` is only the estimate shown on the card.
    proceeds_from_leg: int | None = None
    max_quantity: Quantity | None = None
    max_spend: PricePaise | None = None

    @model_validator(mode="after")
    def _check(self) -> "PlanLeg":
        if self.quantity_basis is QuantityBasis.FROM_PROCEEDS:
            if self.proceeds_from_leg is None or self.proceeds_from_leg >= self.index:
                raise ValueError("FROM_PROCEEDS needs proceeds_from_leg pointing at an earlier leg")
            if self.max_quantity is None or self.max_spend is None:
                raise ValueError("FROM_PROCEEDS needs max_quantity and max_spend caps")
            if self.order.side is not Side.BUY:
                raise ValueError("only a BUY leg can be funded from proceeds")
        elif any(x is not None for x in (self.proceeds_from_leg, self.max_quantity, self.max_spend)):
            raise ValueError("proceeds_from_leg and caps only apply to FROM_PROCEEDS")
        return self


class Plan(Model):
    """Several orders approved together (shown to the trader as a basket order)."""

    id: str
    title: str = ""
    legs: Annotated[list[PlanLeg], Field(min_length=1, max_length=10)]
    on_leg_failure: LegFailurePolicy = LegFailurePolicy.HALT
    state: PlanState = PlanState.PENDING
    created_at: AwareDatetime
    expires_at: AwareDatetime
    # Set while the plan waits for the Co-Captain (app/cocaptain); shown on the ticket, not part of the plan hash.
    co_captain: str | None = None
    co_captain_name: str | None = None
    co_reasons: list[str] = Field(default_factory=list)

    @field_validator("title", mode="before")
    @classmethod
    def _clean_title(cls, v: Any) -> Any:
        return sanitize_text(v) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _check(self) -> "Plan":
        if [leg.index for leg in self.legs] != list(range(len(self.legs))):
            raise ValueError("leg indexes must run 0..n-1 in order")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self

    @computed_field
    @property
    def plan_hash(self) -> str:
        """Binds the approval to every leg, its caps, and the failure policy."""
        return _sha256(
            {
                "legs": [
                    {
                        "index": leg.index,
                        "order": leg.order.order_hash,
                        "basis": leg.quantity_basis.value,
                        "from": leg.proceeds_from_leg,
                        "max_quantity": leg.max_quantity,
                        "max_spend": leg.max_spend,
                    }
                    for leg in self.legs
                ],
                "on_leg_failure": self.on_leg_failure.value,
            }
        )


class PlanLegResult(Model):
    index: Annotated[int, Field(ge=0)]
    label: str = ""  # e.g. "Sell 10 INFY"
    status: LegStatus
    requested_quantity: Annotated[int, Field(ge=0)] = 0
    filled_quantity: Annotated[int, Field(ge=0)] = 0
    avg_fill_price: PricePaise | None = None
    rejection_reason: RejectionReason | None = None
    message: str = ""

    @field_validator("label", "message", mode="before")
    @classmethod
    def _clean_message(cls, v: Any) -> Any:
        return sanitize_text(v, 300) if isinstance(v, str) else v

    @computed_field
    @property
    def pending_quantity(self) -> int:
        if self.status in (LegStatus.OPEN, LegStatus.PARTIAL):
            return max(self.requested_quantity - self.filled_quantity, 0)
        return 0


class PlanReport(Model):
    plan_id: str
    state: PlanState
    legs: list[PlanLegResult]
    summary: str = ""  # plain-English account of every step, written by code

    @field_validator("summary", mode="before")
    @classmethod
    def _clean_summary(cls, v: Any) -> Any:
        return v.strip()[:2000] if isinstance(v, str) else v

    @computed_field
    @property
    def all_filled(self) -> bool:
        return bool(self.legs) and all(leg.status is LegStatus.FILLED for leg in self.legs)


# --------------------------------------------------------------------------- #
# Standing rules
# --------------------------------------------------------------------------- #


class RuleCondition(Model):
    instrument_key: str
    comparator: Comparator
    basis: RuleBasis = RuleBasis.ABSOLUTE
    absolute_price: PricePaise | None = None  # ABSOLUTE
    reference_price: PricePaise | None = None  # other bases: resolved once at creation
    change_pct: float | None = None  # other bases: e.g. -3.0 for "drops 3%"

    @model_validator(mode="after")
    def _check(self) -> "RuleCondition":
        if self.basis is RuleBasis.ABSOLUTE:
            if self.absolute_price is None:
                raise ValueError("ABSOLUTE needs absolute_price")
            if self.reference_price is not None or self.change_pct is not None:
                raise ValueError("ABSOLUTE cannot have reference_price or change_pct")
        else:
            if self.reference_price is None or self.change_pct is None:
                raise ValueError(f"{self.basis.value} needs reference_price and change_pct")
            if self.absolute_price is not None:
                raise ValueError(f"{self.basis.value} cannot have absolute_price")
        return self

    @computed_field
    @property
    def trigger_price(self) -> int:
        if self.basis is RuleBasis.ABSOLUTE:
            assert self.absolute_price is not None
            return self.absolute_price
        assert self.reference_price is not None and self.change_pct is not None
        return round(self.reference_price * (1 + self.change_pct / 100))

    def is_met(self, ltp: int) -> bool:
        """Strict comparison: 'below 3800' is not met at exactly 3800."""
        if self.comparator is Comparator.BELOW:
            return ltp < self.trigger_price
        return ltp > self.trigger_price


class Rule(Model):
    id: str
    kind: RuleKind
    description: str = ""  # the trader's own words, for display
    condition: RuleCondition
    order_template: OrderIntent | None = None  # TRIGGER_ORDER only
    max_gap_pct: float = Field(default=2.0, gt=0)  # past this beyond the trigger, the card warns of a gap
    heads_up_pct: float | None = Field(default=None, gt=0)  # reserved; built later
    status: RuleStatus = RuleStatus.ACTIVE
    created_at: AwareDatetime
    fired_at: AwareDatetime | None = None

    @field_validator("description", mode="before")
    @classmethod
    def _clean_description(cls, v: Any) -> Any:
        return sanitize_text(v, 300) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _check(self) -> "Rule":
        if self.kind is RuleKind.TRIGGER_ORDER:
            if self.order_template is None:
                raise ValueError("TRIGGER_ORDER needs order_template")
            if self.order_template.action is not OrderAction.PLACE:
                raise ValueError("a rule can only place new orders")
        elif self.order_template is not None:
            raise ValueError("ALERT cannot have order_template")
        if (self.status is RuleStatus.FIRED) != (self.fired_at is not None):
            raise ValueError("fired_at is set exactly when status is FIRED")
        return self

    @property
    def client_order_id(self) -> str:
        """Derived from the rule id so a double fire cannot create two distinct orders."""
        return f"rule-{self.id}"


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


class AuditEvent(Model):
    id: str
    ts: AwareDatetime
    kind: AuditKind
    actor: Literal["user", "llm", "system", "broker"]
    subject_id: str | None = None
    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("summary", mode="before")
    @classmethod
    def _clean_summary(cls, v: Any) -> Any:
        return sanitize_text(v, 300) if isinstance(v, str) else v
