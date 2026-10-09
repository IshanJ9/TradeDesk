"""A keyword stand-in for a language model.

It implements the same `LLMClient` interface as a real provider and makes the same tool calls,
so the whole chat flow (tools, guards, cards) runs and is testable without any provider. It
handles plain, self-contained requests only; a real model replaces it for natural language.
"""

import re
from collections.abc import Callable
from itertools import count

from app.llm.types import LLMTurn, Message, ToolCall, ToolSpec

# No figures in here: every answer passes the number check, and a figure that is in neither the
# account data nor the trader's message would be refused.
# (the rules below say what the stand-in understands; a real model understands far more)
HELP = (
    "I can show your holdings, positions, P&L, orders, funds, stock prices and the NIFTY option chain, "
    "and prepare an order card for you to approve. Try asking: show my holdings."
)

_NUM = r"(?P<{n}>\d[\d,]*(?:\.\d+)?)"
_ids = count(1)


def _num(raw: str, suffix: str | None = None) -> float:
    value = float(raw.replace(",", ""))
    suffix = (suffix or "").lower()
    if suffix == "k":
        value *= 1_000
    elif suffix in {"l", "lakh", "lac", "lakhs"}:
        value *= 100_000
    elif suffix in {"cr", "crore"}:
        value *= 10_000_000
    return int(value) if value == int(value) else value


def _clean_name(name: str) -> str:
    name = re.sub(r"\b(shares?|stocks?|of|the|my|please|rs\.?|inr)\b|₹", " ", name, flags=re.I)
    return " ".join(name.split())


_FRACTIONS = {"half": 0.5, "ahalf": 0.5, "athird": 1 / 3, "aquarter": 0.25, "all": 1.0, "everything": 1.0}
_FALL = r"(?:falls?|drops?|dips?|declines?|loses?|slips?|goes\s+down|sinks?)"
_RISE = r"(?:rises?|gains?|climbs?|jumps?|goes\s+up|surges?)"


def _trigger_args(cond: str) -> dict | None:
    """The trigger part of a sentence ('it falls below 3800', 'drops 3% from my buy price'), or None."""
    pct = re.search(rf"\b(?P<verb>{_FALL}|{_RISE})\s+(?:by\s+)?" + _NUM.format(n="pct") + r"\s*%(?P<rest>.*)$", cond)
    if pct:
        below = re.fullmatch(_FALL, pct["verb"]) is not None
        rest = pct["rest"]
        basis = (
            "AVG_BUY" if re.search(r"buy(?:ing)?\s+price|avg|average|cost", rest)
            else "PREV_CLOSE" if re.search(r"previous|yesterday|prev", rest)
            else None
        )
        return dict(comparator="BELOW" if below else "ABOVE", percent=_num(pct["pct"]), basis=basis)
    absolute = re.search(r"\b(?P<dir>below|under|above|over)\s+₹?" + _NUM.format(n="price") + r"\s*$", cond)
    if absolute:
        below = absolute["dir"] in ("below", "under")
        return dict(comparator="BELOW" if below else "ABOVE", price_rupees=_num(absolute["price"]))
    return None


def _call(name: str, **args) -> ToolCall:
    return ToolCall(id=f"rule-{next(_ids)}", name=name, input={k: v for k, v in args.items() if v is not None})


class RuleBasedLLM:
    def __init__(self, renderers: dict[str, Callable[[dict], str]]):
        self._render = renderers

    async def complete(self, *, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMTurn:
        last = messages[-1]
        if last.role == "tool":
            return LLMTurn(text=self._answer(last))
        calls = self.parse(last.text)
        return LLMTurn(tool_calls=calls) if calls else LLMTurn(text=HELP)

    # ---- answering ------------------------------------------------------------------ #

    def _answer(self, tool_message: Message) -> str:
        results = [r for r in tool_message.tool_results if r.name in self._render]
        # holdings/positions are asked for together; drop the empty one when the other has rows
        kinds = {"get_holdings": "holdings", "get_positions": "positions"}
        has_rows = any(r.output.get("count") for r in results if r.name in kinds)
        parts = []
        for r in results:
            if has_rows and r.name in kinds and not r.output.get("count"):
                continue
            parts.append(self._render[r.name](r.output))
        return "\n".join(parts)

    # ---- parsing ---------------------------------------------------------------------- #

    def parse(self, text: str) -> list[ToolCall]:
        raw = text.strip()
        t = raw.lower().rstrip("?.! ")
        if not t:
            return []

        if m := re.match(r"(?:please\s+)?cancel\s+(?:my\s+)?(?:rule|alert|instruction)\s+(?P<id>r-[a-z0-9]+)$", t):
            return [_call("cancel_rule", rule_id=m["id"])]
        if re.match(r"(?:please\s+)?(?:list|show|view|what are|what's|whats)\b.*\b(?:rules|alerts|standing instructions?)$", t):
            return [_call("list_rules")]

        # standing instructions: "alert me if ...", "buy 5 tcs if ..."
        if m := re.match(r"(?:please\s+)?(?:alert|notify|tell|warn|ping)\s+me\s+(?:if|when|once)\s+(?P<rest>.+)$", t):
            subject = re.match(
                rf"(?P<name>.+?)\s+(?:{_FALL}|{_RISE}|goes|gets|is|moves|trades|crosses|below|under|above|over)\b", m["rest"]
            )
            trig = _trigger_args(m["rest"])
            if subject and trig:
                return [_call("create_rule", kind="ALERT", instrument=_clean_name(subject["name"]), **trig)]
        if m := re.match(
            r"(?:please\s+)?(?P<side>buy|sell)\s+" + _NUM.format(n="qty") + r"\s+(?P<name>.+?)\s+(?:if|when|once)\s+(?P<rest>.+)$", t
        ):
            trig = _trigger_args(m["rest"])
            if trig:
                return [_call("create_rule", kind="TRIGGER_ORDER", instrument=_clean_name(m["name"]), side=m["side"].upper(),
                              quantity=int(_num(m["qty"])), product="MIS" if "intraday" in t else "CNC", **trig)]

        # plans: "sell half my infosys and buy itc with the money"
        if m := re.match(
            r"(?:please\s+)?sell\s+(?P<size>half|a\s+half|a\s+third|a\s+quarter|all|everything|\d+(?:\.\d+)?\s*%|\d+)\s+"
            r"(?:of\s+)?(?:my\s+)?(?P<a>.+?)\s+(?:and|then|&)\s+(?:use\s+(?:the\s+)?(?:money|proceeds)\s+(?:to|and)\s+)?"
            r"buy\s+(?P<b>.+?)(?:\s+with\s+(?:the\s+)?(?:money|proceeds|that\s+money))?$",
            t,
        ):
            first = dict(instrument=_clean_name(m["a"]), side="SELL")
            size = m["size"].replace(" ", "")
            if size.isdigit():
                first["quantity"] = int(size)
            else:
                first["fraction_of_holding"] = _FRACTIONS.get(size) or float(size.rstrip("%")) / 100
            second = dict(instrument=_clean_name(m["b"]), side="BUY", proceeds_of_leg=0)
            return [_call("propose_plan", legs=[first, second])]
        if re.search(r"\bplan\b", t) and re.search(r"happen|status|report|how did|how'?s|went|result|filled", t):
            return [_call("get_plan_report")]

        if m := re.match(r"(?:please\s+)?cancel\s+(?:my\s+)?(?:order\s+)?(?P<id>[a-z]*\d[a-z0-9]*)$", t):
            return [_call("propose_order", action="CANCEL", target_order_id=m["id"].upper())]

        if m := re.match(
            r"(?:please\s+)?(?:modify|change|amend)\s+(?:my\s+)?order\s+(?P<id>[a-z]*\d[a-z0-9]*)\s+"
            r"(?:to|price(?:\s+to)?|at|@)\s*₹?" + _NUM.format(n="price") + r"$",
            t,
        ):
            return [_call("propose_order", action="MODIFY", target_order_id=m["id"].upper(), limit_price_rupees=_num(m["price"]))]

        if m := re.match(
            r"(?:please\s+)?(?P<side>buy|sell)\s+(?P<name>.+?)\s+worth\s+₹?" + _NUM.format(n="amt")
            + r"\s*(?P<sfx>k|l|lakh|lakhs|lac|cr|crore)?$",
            t,
        ):
            return [self._order(m["side"], _clean_name(m["name"]), t, amount=_num(m["amt"], m["sfx"]))]

        if m := re.match(
            r"(?:please\s+)?(?P<side>buy|sell)\s+" + _NUM.format(n="qty") + r"\s+(?P<name>.+?)"
            r"(?:\s+(?:at|@|for|limit(?:\s+(?:at|of))?)\s*₹?" + _NUM.format(n="price") + r")?$",
            t,
        ):
            return [
                self._order(
                    m["side"], _clean_name(m["name"]), t, quantity=int(_num(m["qty"])),
                    price=_num(m["price"]) if m["price"] else None,
                )
            ]

        if "option" in t or "near the money" in t or "strike" in t:
            expiry = re.search(r"\d{4}-\d{2}-\d{2}", t)
            return [_call("get_option_chain", underlying="NIFTY", expiry=expiry.group() if expiry else None)]

        threshold = re.search(r"(?:down|fallen|lost|below|loss(?:es)?(?:\s+of)?)\D{0,15}?(\d+(?:\.\d+)?)\s*%", t)
        wants_pnl = bool(re.search(r"p\s?&\s?l|\bpnl\b|profit|how am i doing|how'?s my (?:portfolio|day)|today.s (?:gain|loss|return)", t))
        calls: list[ToolCall] = []
        if wants_pnl:
            calls.append(_call("get_pnl_summary"))
        if threshold:
            pct = float(threshold.group(1))
            calls += [_call("get_holdings", down_more_than_pct=pct), _call("get_positions", down_more_than_pct=pct)]
        if calls:
            return calls

        if m := re.search(r"(?:average|avg)\s+(?:buy(?:ing)?\s+)?price\s+(?:of|for)\s+(?P<name>.+)$", t):
            sym = _clean_name(m["name"])
            return [_call("get_holdings", symbol=sym), _call("get_positions", symbol=sym)]

        if m := re.search(r"(?:price|quote|ltp|rate)\s+(?:of|for)\s+(?P<name>.+)$|how much is\s+(?P<name2>.+)$|(?P<name3>.+?)\s+(?:price|quote|ltp)$", t):
            name = _clean_name(m["name"] or m["name2"] or m["name3"])
            if name:
                return [_call("get_quote", symbol=name)]

        if re.search(r"\b(funds?|cash|balance|margin)\b", t):
            return [_call("get_funds")]
        if re.search(r"\borders?\b", t):
            return [_call("get_orders")]
        if re.search(r"\bpositions?\b", t):
            return [_call("get_positions")]
        if re.search(r"\b(holdings?|portfolio|stocks|shares)\b", t):
            return [_call("get_holdings")]
        return []

    @staticmethod
    def _order(side, name, text, *, quantity=None, amount=None, price=None) -> ToolCall:
        return _call(
            "propose_order",
            action="PLACE",
            instrument=name,
            side=side.upper(),
            quantity=quantity,
            amount_rupees=amount,
            order_type="LIMIT" if price is not None else "MARKET",
            limit_price_rupees=price,
            product="MIS" if "intraday" in text else "CNC",
        )
