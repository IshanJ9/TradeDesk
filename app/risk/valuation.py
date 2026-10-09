"""The portfolio value convention shared by goal capture and the discipline report."""

import asyncio

from app.broker.base import ReadOnlyBroker


async def portfolio_value(broker: ReadOnlyBroker) -> int:
    funds, holdings, positions = await asyncio.gather(
        broker.get_funds(), broker.get_holdings(), broker.get_positions()
    )
    return funds.total + sum(h.current_value for h in holdings) + sum(p.current_value for p in positions)
