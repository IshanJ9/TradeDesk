"""One plain-English line describing exactly what an order card will do ('explain-before-act')."""

from app.schemas import OrderAction, OrderType, PendingOrder, Side, fmt_rupees


def readback(p: PendingOrder) -> str:
    name = f"{p.instrument.name or p.instrument.symbol} ({p.instrument.exchange.value})"
    if p.action is OrderAction.CANCEL:
        return f"You are cancelling order {p.target_order_id}: {p.side.value.lower()} {p.quantity} of {name}."

    verb = "buying" if p.side is Side.BUY else "selling"
    if p.action is OrderAction.MODIFY:
        verb = f"changing your order to {verb}"
    shares = "share" if p.quantity == 1 else "shares"
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
