"""Checks on what the model says, run in plain code after it answers.

- Numbers: every number in an answer must come from a tool result or from the trader's own
  message. Anything else is treated as invented.
- Claims of execution: the model can never place an order, so it must not say it did.
- Advice: the copilot states facts from the account; it does not predict or recommend.
"""

import re
from collections.abc import Iterable
from typing import Any

_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_FREE_SMALL_INT = 10  # counts like "two positions" or "3 orders" need no source


def _parse(raw: str) -> tuple[float, int]:
    cleaned = raw.replace(",", "")
    decimals = len(cleaned.split(".")[1]) if "." in cleaned else 0
    return float(cleaned), decimals


def numbers_in(text: str) -> list[tuple[float, int]]:
    """(value, decimals as written). Signs are ignored; digits inside words/ids (INFY2, MOCK0001) are skipped."""
    return [_parse(m.group()) for m in _NUM.finditer(text)]


def flatten(value: Any) -> Iterable[str]:
    """All strings and numbers inside a JSON-like value, as text."""
    if isinstance(value, dict):
        for v in value.values():
            yield from flatten(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from flatten(v)
    elif isinstance(value, bool) or value is None:
        return
    elif isinstance(value, (int, float)):
        yield repr(abs(value)) if isinstance(value, float) else str(abs(value))
    else:
        yield str(value)


def ungrounded_numbers(answer: str, sources: Iterable[Any], user_text: str = "") -> list[str]:
    """Numbers in `answer` that appear in none of `sources` (tool outputs) or `user_text`."""
    allowed: list[tuple[float, int]] = numbers_in(user_text)
    for source in sources:
        for chunk in flatten(source):
            allowed.extend(numbers_in(chunk))
    bad = []
    for value, decimals in numbers_in(answer):
        if value == int(value) and value <= _FREE_SMALL_INT and decimals == 0:
            continue
        # a stated number is fine if it is a source number, or a source number rounded to the
        # precision the answer used ("-7.2%" for -7.24, "1,450" for 1450.00)
        if any(value == a or value == round(a, decimals) for a, _ in allowed):
            continue
        bad.append(f"{value:g}")
    return bad


_EXECUTION_CLAIM = re.compile(
    r"\b(i|we)(\s+have|'ve|\s+just)?\s+(placed|sent|submitted|executed|bought|sold|cancel(?:l)?ed|modified)\b"
    r"|\b(your\s+)?order\s+(has\s+been|was|is\s+now)\s+(placed|sent|submitted|executed|filled|cancel(?:l)?ed)\b"
    r"|\b(done|all\s+set)\b.{0,20}\b(bought|sold|ordered)\b",
    re.IGNORECASE,
)


def claims_execution(text: str) -> bool:
    return bool(_EXECUTION_CLAIM.search(text))


_ADVICE = re.compile(
    r"\byou\s+(should|must|ought\s+to|need\s+to)\s+(buy|sell|hold|exit|short|accumulate)\b"
    r"|\b(i|we)\s+(recommend|suggest|advise)\b"
    r"|\b(good|great|best|right|perfect)\s+time\s+to\s+(buy|sell)\b"
    r"|\b(will|is\s+going\s+to|likely\s+to|expected\s+to|set\s+to)\s+(rise|fall|go\s+up|go\s+down|rally|crash|drop|increase|decrease|surge)\b"
    r"|\b(target\s+price|price\s+target|stock\s+tip|hot\s+stock|multibagger|can't\s+miss|buy\s+now|sell\s+now)\b",
    re.IGNORECASE,
)


def gives_advice(text: str) -> bool:
    return bool(_ADVICE.search(text))


def plain_text(text: str) -> str:
    """The chat shows plain text, so remove markdown a model may add (bold, headings, tables, code ticks) and the
    odd typographic spaces it likes. Changes how an answer looks, never what it says."""
    out = []
    for line in text.replace(" ", " ").replace(" ", " ").replace("‑", "-").splitlines():
        stripped = line.strip()
        if re.fullmatch(r"\|?[\s:|-]+\|?", stripped) and "-" in stripped and "|" in stripped:
            continue  # a table's separator row (|---|---|)
        if stripped.startswith("|") and stripped.endswith("|"):
            line = " | ".join(cell.strip() for cell in stripped.strip("|").split("|"))
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)  # headings
        line = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), line)  # bold
        line = line.replace("`", "")
        out.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# ---- the numbers a model puts into an ORDER must be numbers the trader actually wrote ------------------
from decimal import Decimal  # noqa: E402

_SCALES = {"k": 1_000, "l": 100_000, "lac": 100_000, "lacs": 100_000, "lakh": 100_000, "lakhs": 100_000,
           "cr": 10_000_000, "crore": 10_000_000, "crores": 10_000_000}
_TYPED = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(k|lakhs?|lacs?|l|crores?|cr)?\b", re.IGNORECASE)
# If the trader wrote a number as a word we cannot check it against the digits, so the check stands down.
_NUMBER_WORDS = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|sixty|"
    r"seventy|eighty|ninety|hundred|thousand|ek|do|teen|char|paanch|panch|chhe|saat|aath|nau|das|bees|pachas|sau|hazaar|hazar)\b",
    re.IGNORECASE,
)


def typed_numbers(texts: Iterable[str]) -> set[Decimal]:
    """Every number written in these messages, including '10k', '2 lakh' and '1,00,000' as their full values."""
    found: set[Decimal] = set()
    for text in texts:
        for m in _TYPED.finditer(text):
            base = Decimal(m.group(1).replace(",", ""))
            found.add(base)
            if m.group(2):
                found.add(base * _SCALES[m.group(2).lower()])
    return found


def numbers_not_typed(named: dict[str, float | int | None], texts: list[str]) -> list[tuple[str, Decimal]]:
    """The (name, value) pairs that the trader never wrote. Empty = all fine (or not checkable)."""
    if any(_NUMBER_WORDS.search(t) for t in texts):
        return []
    typed = typed_numbers(texts)
    return [(k, Decimal(str(v))) for k, v in named.items() if v is not None and Decimal(str(v)) not in typed]
