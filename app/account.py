import asyncio

from app.api_models import AccountSnapshot
from app.broker.base import BrokerAdapter


async def build_account(broker: BrokerAdapter) -> AccountSnapshot:
    funds, holdings, positions, locks = await asyncio.gather(
        broker.get_funds(), broker.get_holdings(), broker.get_positions(), broker.get_account_locks()
    )
    return AccountSnapshot(funds=funds, holdings=holdings, positions=positions, locks=locks)
