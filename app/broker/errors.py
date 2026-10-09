"""Turning a failed 021 order call into the right typed error.

This matters because the two errors mean opposite things to the safety layer:

- `BrokerRejected`: the broker looked at the order and said no. It is definitely NOT placed, so the
  trader can be told "rejected" and may try again with a new card.
- `BrokerTimeout`: we cannot tell whether the order was placed. It is reconciled against the order
  book and never retried blindly.

The cost of getting it wrong is not symmetric. Calling a rejection "unknown" only delays the answer.
Calling an unknown "rejected" invites the trader to place the order again, and if the first one did go
through, they now own two. So: only call something a rejection when the broker clearly said so.

What 021's API guide says (HTTP status -> meaning):
  400 bad body or unknown exchange/product/book/validity   -> not processed
  401 missing/expired token, or account not allowed         -> not processed
  403 account inactive                                      -> not processed
  421 account belongs to another cluster                    -> not processed
  422 blocked by safe mode (the text says why)              -> not processed
  500 'rejected by risk checks, or a server error'          -> AMBIGUOUS: read the text
  503 temporarily unavailable                               -> AMBIGUOUS for an order (it may have got in)
Rate limits and anything else (including a dropped connection) are treated as unknown.
"""

from app.broker.base import BrokerError, BrokerRejected, BrokerTimeout
from app.schemas import RejectionReason

_DEFINITELY_NOT_PROCESSED = {400, 401, 403, 421, 422}

# Wording that identifies a real risk-check refusal, mapped to our reasons. Anything else on a 500
# (empty text, "internal server error", a stack trace, a message we don't recognise) stays UNKNOWN.
_RISK_WORDS: tuple[tuple[tuple[str, ...], RejectionReason], ...] = (
    (("insufficient", "not enough fund", "low fund", "margin"), RejectionReason.INSUFFICIENT_FUNDS),
    (("circuit", "price band", "outside the range", "dpr"), RejectionReason.PRICE_BAND),
    (("tick",), RejectionReason.INVALID_PRICE),
    (("lot size", "lot multiple", "freeze", "quantity"), RejectionReason.INVALID_QUANTITY),
    (("market is closed", "market closed", "outside market hours"), RejectionReason.MARKET_CLOSED),
    (("suspended", "not allowed to trade", "banned"), RejectionReason.SUSPENDED),
    (("trigger", "risk", "rms", "rejected by", "not allowed"), RejectionReason.RISK_CHECK),
)


def reason_from_text(text: str | None) -> RejectionReason | None:
    """A rejection reason if the text clearly describes one, else None."""
    lowered = (text or "").lower()
    for words, reason in _RISK_WORDS:
        if any(w in lowered for w in words):
            return reason
    return None


def classify_order_failure(status_code: int | None, error_text: str | None = None) -> BrokerError:
    """The error to raise for an order call that did not come back as a success.

    `status_code` is None for a dropped connection or a client-side timeout.
    """
    text = (error_text or "").strip()
    if status_code in _DEFINITELY_NOT_PROCESSED:
        return BrokerRejected(reason_from_text(text) or RejectionReason.OTHER, text or f"HTTP {status_code}")
    if status_code == 500:
        reason = reason_from_text(text)
        if reason is not None:
            return BrokerRejected(reason, text)
        return BrokerTimeout(f"HTTP 500 without a clear refusal: {text[:120] or 'no message'}")
    return BrokerTimeout(f"HTTP {status_code}" if status_code else "no response")
