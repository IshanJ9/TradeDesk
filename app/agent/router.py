"""Decides which tools a message may use. Plain code, no model call.

A question ("what's my P&L?") gets the READ route: the model is offered only tools that read. It cannot
prepare an order card, a rule or a plan, however it is asked or tricked. Anything that might be an action
gets the ACT route and every tool, exactly as the classic copilot has. When unsure, ACT: the approval card
and the Approve click still stand between any draft and the broker.

The router only chooses tools. It never decides prices, quantities or whether anything is sent.
"""

import re
from typing import Literal

Route = Literal["read", "act"]

_ACTION = re.compile(
    r"\b(buy|sell|purchase|acquire|exit|square|close|cancel|modify|change|amend|move|update|stop|sl|trail\w*|"
    r"alert|notify|remind|warn|tell me (?:when|if)|let me know|ping|watch|"
    r"rebalance|trim|reduce|cut|book|dump|get rid|offload|unload|liquidat\w*|add|accumulate|invest|put|"
    r"place|order(?:s)? to|plan|rule|limit|target|half|quarter|third|all my|"
    r"kharid\w*|bech\w*|lena|lelo|nikal\w*)\b"
    r"|\bif\b.*\b(falls?|drops?|rises?|goes|crosses|hits|reaches|below|above)\b",
    re.IGNORECASE,
)


def route(message: str) -> Route:
    return "act" if _ACTION.search(message) else "read"
