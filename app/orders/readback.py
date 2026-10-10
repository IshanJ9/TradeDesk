"""One plain-English line describing exactly what an order card will do ('explain-before-act')."""

from app.schemas import OrderAction, OrderType, PendingOrder, Side, fmt_rupees


def readback(p: PendingOrder) -> str:
    name = f"{p.instrument.label} ({p.instrument.exchange.value})"
    if p.action is OrderAction.CANCEL:
        return f"You are cancelling order {p.target_order_id}: {p.side.value.lower()} {p.quantity} of {name}."

    verb = "buying" if p.side is Side.BUY else "selling"
    if p.action is OrderAction.MODIFY:
        verb = f"changing your order to {verb}"
    shares = "share" if p.quantity == 1 else "shares"
    if p.instrument.is_option:  # options are counted in lots: "1 lot (75 units) of NIFTY 24,500 CE (13 Oct 2026)"
        lots = p.quantity // (p.instrument.lot_size or 1)
        return _option_readback(p, verb, lots, name)
    if p.order_type is OrderType.STOP_LIMIT:
        move = "falls to" if p.side is Side.SELL else "rises to"
        edge = "at least" if p.side is Side.SELL else "up to"
        total = f", about {fmt_rupees(p.est_total)} {'after' if p.side is Side.SELL else 'including'} charges" if p.est_total else ""
        return (
            f"You are setting a stop-loss: if {p.instrument.symbol} {move} {fmt_rupees(p.trigger_price)}, "
            f"{verb} {p.quantity} {shares} of {name} {edge} {fmt_rupees(p.limit_price)}{total}. "
            "Nothing is traded until that price is reached."
        )
    if p.order_type is OrderType.LIMIT or p.limit_price is not None:
        price = f"at {'up to' if p.side is Side.BUY else 'at least'} {fmt_rupees(p.limit_price)}"
    else:
        bound = fmt_rupees(p.protection_price) if p.protection_price else "the market price"
        price = f"at the market price, protected so it never fills {'above' if p.side is Side.BUY else 'below'} {bound}"
    total = ""
    if p.est_total:
        total = f", total about {fmt_rupees(p.est_total)} including charges" if p.side is Side.BUY else (
            f", about {fmt_rupees(p.est_total)} after charges"
        )
    return f"You are {verb} {p.quantity} {shares} of {name} {price}{total}."


def _option_readback(p: PendingOrder, verb: str, lots: int, name: str) -> str:
    size = f"{lots} lot{'s' if lots != 1 else ''} ({p.quantity} units)"
    if p.order_type is OrderType.STOP_LIMIT:
        move = "falls to" if p.side is Side.SELL else "rises to"
        return (f"You are setting a stop: if the premium {move} {fmt_rupees(p.trigger_price)}, {verb} {size} of {name} "
                f"at {'at least' if p.side is Side.SELL else 'up to'} {fmt_rupees(p.limit_price)} per unit. "
                "Nothing is traded until then.")
    if p.limit_price is not None:
        price = f"at a premium of {'up to' if p.side is Side.BUY else 'at least'} {fmt_rupees(p.limit_price)} per unit"
    else:
        price = (f"at the market premium, protected so it never fills {'above' if p.side is Side.BUY else 'below'} "
                 f"{fmt_rupees(p.protection_price)} per unit")
    total = (f", total about {fmt_rupees(p.est_total)} including charges" if p.side is Side.BUY
             else f", about {fmt_rupees(p.est_total)} after charges") if p.est_total else ""
    return f"You are {verb} {size} of {name} {price}{total}."
