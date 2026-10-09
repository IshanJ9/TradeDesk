"""021's instrument file -> the instruments the app trades.

The file is a gzipped CSV of about 1,05,000 rows (NSE + BSE, cash and F&O) with these columns:
token, exchange, underlying_token, underlying_exchange, min_lot_size, board_lot_quantity, freeze_quantity,
lower_circuit, upper_circuit, expiry, option_type, strike_price, symbol, instrument_type, isin, ticksize.

What it does NOT have: company names and a series (EQ/BE). So names come from a small table
(`names.py`) and every cash stock is treated as series EQ. Prices are paise already.

Only what this app uses is turned into `Instrument` objects: NSE and BSE cash stocks, the indices, and
(lazily, per underlying) the option contracts for the option chain.
"""

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from pydantic import ValidationError

from app.broker.zerotwoone.names import NAMES
from app.schemas import Exchange, Instrument, OptionType

log = logging.getLogger("tradedesk.instruments")

# 021 names -> what we call them. Orders use the short request name; the file and responses use these.
EXCHANGE_BY_FILE_NAME = {"NSECM": Exchange.NSE, "BSEEQ": Exchange.BSE}
WS_CODE = {"NSECM": 1, "NSEFO": 2, "NSEIDX": 3, "BSEEQ": 4, "BSEEQD": 5, "BSEIDX": 6}
REQUEST_NAME = {"NSECM": "NSE", "NSEFO": "NSEFO", "BSEEQ": "BSE", "BSEEQD": "BSEFO"}
OPTION_TYPES = {"CALL": OptionType.CE, "PUT": OptionType.PE}

_EXCHANGE_EPOCH_OFFSET = 315_513_000  # add to exchange seconds-since-1980 to get Unix seconds (guide)


@dataclass(frozen=True)
class Listing:
    """One tradable thing: our Instrument plus what 021 needs to be told about it."""

    token: int
    exchange: str  # file name: NSECM, NSEFO, NSEIDX, BSEEQ
    instrument: Instrument
    board_lot: int = 1
    freeze_quantity: int = 0

    @property
    def ws(self) -> tuple[int, int]:
        """[exchangeCode, token] as the market socket wants it."""
        return WS_CODE[self.exchange], self.token

    @property
    def request_exchange(self) -> str:
        return REQUEST_NAME[self.exchange]


def expiry_to_date(raw: int, today: date | None = None) -> date | None:
    """The guide says 'an integer timestamp from the exchange' without naming the epoch. Exchange times
    elsewhere in the guide are seconds since 1980, so try that first, then plain Unix seconds.

    The two readings of one number differ by ten years, so a window around today settles it: a live contract
    expires between a week ago and three years from now. None if neither reading fits (the row is skipped)."""
    today = today or datetime.now(timezone.utc).date()
    lo, hi = today - timedelta(days=7), today + timedelta(days=3 * 366)
    for offset in (_EXCHANGE_EPOCH_OFFSET, 0):
        try:
            d = datetime.fromtimestamp(raw + offset, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            continue
        if lo <= d <= hi:
            return d
    return None


def option_symbol(underlying: str, expiry: date, strike_paise: int, kind: OptionType) -> str:
    strike = str(strike_paise // 100) if strike_paise % 100 == 0 else f"{Decimal(strike_paise) / 100:.2f}"
    return f"{underlying}{expiry:%y%m%d}{strike}{kind.value}"


def _int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class InstrumentMaster:
    def __init__(
        self,
        listings: list[Listing],
        option_rows: list[dict],
        index_tokens: dict[tuple[str, int], str],
        today: date | None = None,
    ):
        self._today = today
        self._by_key: dict[str, Listing] = {}
        self._by_token: dict[tuple[str, int], Listing] = {}
        for item in listings:
            self._by_key.setdefault(item.instrument.key, item)  # first token wins on a duplicate symbol
            self._by_token[(item.exchange, item.token)] = item
        self._equities = [i for i in self._by_key.values() if i.exchange in EXCHANGE_BY_FILE_NAME]
        self._option_rows = option_rows
        self._index_tokens = index_tokens
        self._options_cache: dict[str, list[Listing]] = {}

    # ---- building ---------------------------------------------------------------------------- #

    @classmethod
    def from_csv(cls, text: str, today: date | None = None) -> "InstrumentMaster":
        listings: list[Listing] = []
        option_rows: list[dict] = []
        index_tokens: dict[tuple[str, int], str] = {}
        skipped = 0
        for row in csv.DictReader(io.StringIO(text)):
            exch, kind = (row.get("exchange") or "").strip(), (row.get("instrument_type") or "").strip()
            symbol = (row.get("symbol") or "").strip().upper().replace(" ", "")
            token = _int(row.get("token", ""), -1)
            if token < 0 or not symbol:
                skipped += 1
                continue
            if kind in ("OPTIDX", "OPTSTK") and exch in ("NSEFO", "BSEEQD"):
                option_rows.append(row)
                continue
            is_stock = kind == "STK" and exch in EXCHANGE_BY_FILE_NAME
            is_index = kind == "IDX" and exch == "NSEIDX"
            if not (is_stock or is_index):
                continue
            tick = _int(row.get("ticksize", ""), 0) or 5
            low, high = _int(row.get("lower_circuit", "")), _int(row.get("upper_circuit", ""))
            try:
                inst = Instrument(
                    symbol=symbol,
                    exchange=EXCHANGE_BY_FILE_NAME.get(exch, Exchange.NSE),
                    series="INDEX" if is_index else "EQ",
                    isin=(row.get("isin") or "").strip() or None,
                    name=NAMES.get(symbol, "") if is_stock and exch == "NSECM" else "",
                    tick_size=tick,
                    price_band_low=low if low > 0 and high > 0 else None,
                    price_band_high=high if low > 0 and high > 0 else None,
                )
            except ValidationError:
                skipped += 1
                continue
            listings.append(
                Listing(token, exch, inst, max(_int(row.get("board_lot_quantity", ""), 1), 1), _int(row.get("freeze_quantity", "")))
            )
            if is_index:
                index_tokens[(exch, token)] = symbol
        if skipped:
            log.info("skipped %d unusable rows in the instrument file", skipped)
        return cls(listings, option_rows, index_tokens, today)

    # ---- lookups ------------------------------------------------------------------------------ #

    def __len__(self) -> int:
        return len(self._by_key)

    def get(self, instrument_key: str) -> Listing | None:
        return self._by_key.get(instrument_key)

    def by_token(self, exchange: str, token: int) -> Listing | None:
        return self._by_token.get((exchange, token))

    def by_symbol(self, symbol: str, exchange: str = "NSECM") -> Listing | None:
        key_exchange = EXCHANGE_BY_FILE_NAME.get(exchange, Exchange.NSE)
        return self._by_key.get(f"{key_exchange.value}:{symbol.upper()}")

    def by_isin(self, isin: str) -> Listing | None:
        for item in self._equities:
            if item.instrument.isin == isin and item.exchange == "NSECM":
                return item
        return None

    def search(self, query: str, limit: int = 10) -> list[Instrument]:
        """Symbol prefix, or every word typed starts some word of the company name (when we know it)."""
        q = query.strip().lower()
        if not q:
            return []
        compact, words = q.replace(" ", ""), q.split()
        exact: list[Instrument] = []
        rest: list[Instrument] = []
        for item in self._equities:
            inst = item.instrument
            name_words = inst.name.lower().split()
            if inst.symbol.lower() == compact:
                exact.append(inst)
            elif inst.symbol.lower().startswith(compact) or (
                name_words and all(any(w.startswith(t) for w in name_words) for t in words)
            ):
                rest.append(inst)
        index = self._by_key.get(f"NSE:{compact.upper()}")
        found = exact + ([index.instrument] if index and index.exchange == "NSEIDX" and index.instrument not in exact else []) + rest
        found.sort(key=lambda i: (i.symbol.lower() != compact, i.exchange is not Exchange.NSE, len(i.symbol)))
        return found[:limit]

    # ---- options (built on demand: there are about a lakh of them) ------------------------------ #

    def _options(self, underlying: str) -> list[Listing]:
        underlying = underlying.upper()
        if underlying not in self._options_cache:
            built: list[Listing] = []
            for row in self._option_rows:
                token = _int(row["token"], -1)
                under = self._index_tokens.get((row["underlying_exchange"].strip(), _int(row["underlying_token"], -1)))
                if under is None:
                    stock = self._by_token.get((row["underlying_exchange"].strip(), _int(row["underlying_token"], -1)))
                    under = stock.instrument.symbol if stock else None
                if under != underlying:
                    continue
                expiry, kind = expiry_to_date(_int(row["expiry"]), self._today), OPTION_TYPES.get((row["option_type"] or "").strip())
                strike, lot = _int(row["strike_price"]), max(_int(row["board_lot_quantity"], 1), 1)
                if expiry is None or kind is None or strike <= 0 or token < 0:
                    continue
                try:
                    inst = Instrument(
                        symbol=option_symbol(underlying, expiry, strike, kind),
                        exchange=Exchange.NSE,
                        series="OPT",
                        tick_size=_int(row["ticksize"]) or 5,
                        underlying=underlying,
                        lot_size=lot,
                        expiry=expiry,
                        strike=strike,
                        option_type=kind,
                    )
                except ValidationError:
                    continue
                built.append(Listing(token, row["exchange"].strip(), inst, lot, _int(row["freeze_quantity"])))
            self._options_cache[underlying] = built
        return self._options_cache[underlying]

    def option_expiries(self, underlying: str, today: date | None = None) -> list[date]:
        found = {o.instrument.expiry for o in self._options(underlying)}
        return sorted(d for d in found if d is not None and (today is None or d >= today))

    def option_listings(self, underlying: str, expiry: date) -> list[Listing]:
        return [o for o in self._options(underlying) if o.instrument.expiry == expiry]
