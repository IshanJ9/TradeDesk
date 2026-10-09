"""021's market-data socket: binary, big-endian frames holding many packets back to back.

Every packet starts with a 2-byte transaction code (TC):
  1  last price (modes ltp / ltpo / ltpc), 12 bytes; snapshot frames may add 5 bytes ('o' or 'c' + price)
  3  full packet, length depends on the exchange code at offset 2
  9  option chain (mode oc), self-describing: a count, then (id, value) pairs
  10 heartbeat, 2 bytes, ignored

A packet we do not understand ends the frame (we cannot know how long it is), and is counted, not guessed.
Prices are paise.
"""

import json
import struct
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class LtpPacket:
    exchange: int
    token: int
    ltp: int
    open: int | None = None
    prev_close: int | None = None


@dataclass(frozen=True)
class FullPacket:
    exchange: int
    token: int
    ltp: int
    prev_close: int
    open: int
    high: int
    low: int
    bid: int | None = None
    ask: int | None = None


@dataclass(frozen=True)
class ChainPacket:
    exchange: int
    token: int
    ltp: int | None = None
    oi: int | None = None
    volume: int | None = None


Packet = LtpPacket | FullPacket | ChainPacket

# exchange code -> (length, token offset, ltp offset, first of close/open/high/low, depth offset, depth level size)
_FULL_LAYOUTS: dict[int, tuple[int, int, int, int, int | None, int]] = {
    1: (220, 4, 8, 44, 64, 14),  # NSE cash
    2: (234, 6, 10, 46, 66, 14),  # NSE F&O
    3: (36, 4, 8, 12, None, 0),  # NSE index: the 'LTP' is the index value
    4: (262, 4, 8, 48, 68, 16),  # BSE cash
    5: (262, 4, 8, 48, 68, 16),  # BSE F&O
    6: (262, 4, 8, 48, 68, 16),  # BSE index
}
# option-chain field id -> value size in bytes
_CHAIN_FIELD_SIZES = {1: 4, 2: 8, 3: 8, 4: 4, 7: 32, 8: 20, 9: 4}

TC_LTP, TC_FULL, TC_CHAIN, TC_HEARTBEAT = 1, 3, 9, 10


def _u(data: bytes, offset: int, size: int) -> int:
    return int.from_bytes(data[offset : offset + size], "big")


def _best(data: bytes, depth: int | None, level: int, level_size: int, index: int) -> int | None:
    if depth is None:
        return None
    price = _u(data, depth + index * level_size + 8, 4)  # each level: quantity (8), price (4), orders
    return price or None


def decode_frame(data: bytes) -> tuple[list[Packet], int]:
    """(packets, bytes we could not make sense of). Never raises on malformed input."""
    packets: list[Packet] = []
    pos = 0
    while pos + 2 <= len(data):
        tc = _u(data, pos, 2)
        if tc == TC_HEARTBEAT:
            pos += 2
        elif tc == TC_LTP:
            if pos + 12 > len(data):
                break
            exchange, token, ltp = _u(data, pos + 2, 2), _u(data, pos + 4, 4), _u(data, pos + 8, 4)
            pos += 12
            extra_open = extra_close = None
            if pos + 5 <= len(data) and data[pos] in (0x6F, 0x63):  # snapshot frames carry open / previous close
                value = _u(data, pos + 1, 4)
                extra_open, extra_close = (value, None) if data[pos] == 0x6F else (None, value)
                pos += 5
            packets.append(LtpPacket(exchange, token, ltp, extra_open, extra_close))
        elif tc == TC_FULL:
            if pos + 4 > len(data):
                break
            layout = _FULL_LAYOUTS.get(_u(data, pos + 2, 2))
            if layout is None or pos + layout[0] > len(data):
                break
            length, token_off, ltp_off, ohlc_off, depth, level_size = layout
            chunk = data[pos : pos + length]
            packets.append(
                FullPacket(
                    exchange=_u(chunk, 2, 2),
                    token=_u(chunk, token_off, 4),
                    ltp=_u(chunk, ltp_off, 4),
                    prev_close=_u(chunk, ohlc_off, 4),
                    open=_u(chunk, ohlc_off + 4, 4),
                    high=_u(chunk, ohlc_off + 8, 4),
                    low=_u(chunk, ohlc_off + 12, 4),
                    bid=_best(chunk, depth, 0, level_size, 0),
                    ask=_best(chunk, depth, 0, level_size, 5),
                )
            )
            pos += length
        elif tc == TC_CHAIN:
            if pos + 9 > len(data):
                break
            token, exchange, count = _u(data, pos + 2, 4), _u(data, pos + 6, 2), data[pos + 8]
            cursor, fields, ok = pos + 9, {}, True
            for _ in range(count):
                if cursor >= len(data) or data[cursor] not in _CHAIN_FIELD_SIZES:
                    ok = False
                    break
                size = _CHAIN_FIELD_SIZES[data[cursor]]
                if cursor + 1 + size > len(data):
                    ok = False
                    break
                fields[data[cursor]] = _u(data, cursor + 1, size)
                cursor += 1 + size
            if not ok:
                break
            packets.append(ChainPacket(exchange, token, ltp=fields.get(1), oi=fields.get(2), volume=fields.get(3)))
            pos = cursor
        else:
            break
    return packets, len(data) - pos


def subscribe_message(task: str, mode: str, instruments: Sequence[tuple[int, int]], filters: str | None = None) -> str:
    """The text frame for subscribe / unsubscribe. `instruments` are (exchange code, token) pairs."""
    body: dict = {"Task": task, "Mode": mode, "Instruments": [[e, t] for e, t in instruments]}
    if filters:
        body["Filters"] = filters
    return json.dumps(body, separators=(",", ":"))


def exchange_time_to_unix(seconds_since_1980: int) -> int:
    return seconds_since_1980 + 315_513_000
