"""Decides which tools a message may use. Plain code, no model call.

Each message takes one of five routes, and each route offers the model a fixed set of tools:

    read   a question about the account          tools that read
    risk   a question about the trader's limits  the profile, discipline report and account summaries
    order  one order, modify or cancel           tools that read + propose_order
    rule   an alert or standing instruction      tools that read + create_rule, cancel_rule, alert_on_holdings
    plan   several orders, or the whole account  tools that read + propose_plan, exit_losing_positions,
                                                 trim_to_max_weight, propose_order

A tool outside the route is refused in code, however the model is asked or tricked. A question can never
draft a card; an order request can never save a standing rule; only a plan request can reach the
whole-portfolio tools. When a message fits several routes, the order of the checks below decides
(rule, then plan, then risk unless an order verb appears, then order, then read). Every drafting route
still ends at an approval card:
the router only chooses tools and never decides prices, quantities or whether anything is sent.
"""

import re
from typing import Literal

Route = Literal["read", "risk", "order", "rule", "plan"]
ROUTES: tuple[Route, ...] = ("read", "risk", "order", "rule", "plan")

# verbs that act on an order, in English and Hinglish
_VERB = (
    r"buy|sell|purchase|acquire|exit|square|close|cancel|modify|amend|book|dump|get rid|offload|unload|liquidat\w*|"
    r"accumulate|invest|add|put|place|trim|reduce|cut|rebalance|change|move|update|raise|lower|set|trail\w*|"
    r"kharid\w*|bech\w*|lena|lelo|nikal\w*"
)
_STRONG_VERB = r"buy|sell|purchase|exit|square|close|cancel|modify|amend|place|kharid\w*|bech\w*|nikal\w*|lelo|lena"

_RULE = re.compile(
    r"\b(?:alert|notify|remind|warn|ping|watch|rules?|standing|instructions?|tell me (?:when|if)|let me know)\b"
    r"|\b(?:if|when|once)\b.*\b(?:falls?|drops?|rises?|goes|crosses|hits|reaches|below|above|under|over)\b",
    re.IGNORECASE,
)
_PLAN = re.compile(
    r"\bplan\b|\b(?:rebalanc\w*|trim)\b"
    rf"|\b(?:{_VERB})\b.*\b(?:all|every|each|any)\s+(?:of\s+)?(?:my\s+)?(?:positions?|holdings?|stocks?|shares?|losers?|losing)\b"
    rf"|\b(?:{_VERB})\b.*(?:\blos(?:ing|ers?)\b|\bin\s+(?:a\s+)?loss\b)"
    r"|\bsell\b.*\b(?:and|then|&)\b.*\bbuy\b|\bwith\s+the\s+(?:money|proceeds)\b"
    r"|\b(?:cap|reduce)\b.*\b(?:exceeds?|above|over|more\s+than|max(?:imum)?|at\s+most)\s+\d+(?:\.\d+)?\s*%",
    re.IGNORECASE,
)
_ORDER = re.compile(rf"\b(?:{_VERB})\b|\bstop[\s-]?loss\b|\border(?:s)?\s+(?:to|for)\b", re.IGNORECASE)
_STRONG = re.compile(rf"\b(?:{_STRONG_VERB})\b", re.IGNORECASE)
_RISK = re.compile(
    r"\b(?:risk|discipline|profile|limits?|goals?|pace|patterns?|overtrad\w*|cooling|streak|mindful|charges)\b", re.IGNORECASE
)

# what each route offers on top of (or instead of) the tools that read
RISK_TOOLS = frozenset({"get_risk_profile", "get_discipline", "get_pnl_summary", "get_orders", "get_positions", "get_holdings", "get_funds"})
DRAFTING_TOOLS: dict[Route, frozenset[str]] = {
    "read": frozenset(),
    "risk": frozenset(),
    "order": frozenset({"propose_order"}),
    "rule": frozenset({"create_rule", "cancel_rule", "alert_on_holdings"}),
    "plan": frozenset({"propose_plan", "exit_losing_positions", "trim_to_max_weight", "propose_order"}),
}

# the checks in order (route, pattern, condition), so the explanation can never drift from the code
PATTERNS: list[tuple[Route, str, str]] = [
    ("rule", _RULE.pattern, "matches"),
    ("plan", _PLAN.pattern, "matches"),
    ("risk", _RISK.pattern, f"matches, unless an order verb appears: {_STRONG.pattern}"),
    ("order", _ORDER.pattern, "matches"),
    ("read", "", "anything else"),
]


def route(message: str) -> Route:
    if _RULE.search(message):
        return "rule"
    if _PLAN.search(message):
        return "plan"
    if _RISK.search(message) and not _STRONG.search(message):
        return "risk"  # "set my goal", "change my limits": no tool can change settings, so it reads
    if _ORDER.search(message):
        return "order"
    return "read"


def allowed_tools(r: Route, read_only: set[str]) -> frozenset[str]:
    """The tool names a route may use, given the names of the tools that only read."""
    reads = RISK_TOOLS & read_only if r == "risk" else frozenset(read_only)
    return reads | DRAFTING_TOOLS[r]


def describe(r: Route) -> str:
    """One line for the live trace."""
    return {
        "read": "question: tools that read",
        "risk": "your limits and discipline: tools that read",
        "order": "one order: may draft an order card for your approval",
        "rule": "alert or standing instruction: may save a rule (it only alerts or prepares a card)",
        "plan": "several orders: may draft a plan for your approval",
    }[r]
