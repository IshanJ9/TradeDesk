import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    broker: str = "mock"  # mock | zerotwoone
    demo_mode: bool = False  # enables /api/chaos/* and /api/locks/* toggles
    database_url: str = "sqlite:///:memory:"
    llm_provider: str = "rules"  # rules = built-in keyword parser; real providers are added in app/llm/factory.py
    approval_ttl_seconds: int = 60
    rule_card_ttl_seconds: int = 900  # a card made by a fired rule waits longer: the trader may be away
    max_active_rules: int = 50
    plan_fill_timeout_seconds: float = 15.0  # how long a plan waits for a step to fill before reporting it open
    plan_poll_interval: float = 0.5
    drift_limit_pct: float = 1.0  # re-confirm if price moved more than this since the card
    market_protection_pct: float = 1.0  # MARKET orders are sent as limits this far from LTP
    # Hard limits. Defaults are 021's published rejection limits; tighten for demos.
    max_order_quantity: int = 100_000
    max_order_value_rupees: int = 10_000_000  # Rs 1 crore
    ticker_interval: float | None = 1.0  # mock price feed period; None = no automatic ticks
    account_push_interval: float = 1.0  # min gap between live account/order pushes
    reconcile_interval: float | None = 5.0  # how often unresolved executions are re-checked
    reconcile_grace_seconds: float = 30.0  # wait this long before calling an order "never sent"
    timeout_reconcile_attempts: int = 3  # order-book lookups right after a timed-out send
    timeout_reconcile_delay: float = 0.2  # seconds between those lookups

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        env = os.environ
        return cls(
            broker=env.get("BROKER", "mock").strip().lower() or "mock",
            demo_mode=_flag("DEMO_MODE", False),
            database_url=env.get("DATABASE_URL", "sqlite:///./tradedesk.db"),
            llm_provider=env.get("LLM_PROVIDER", "").strip().lower() or "rules",
            approval_ttl_seconds=int(env.get("APPROVAL_TTL_SECONDS", "60")),
            drift_limit_pct=float(env.get("DRIFT_LIMIT_PCT", "1.0")),
            market_protection_pct=float(env.get("MARKET_PROTECTION_PCT", "1.0")),
            max_order_quantity=int(env.get("MAX_ORDER_QUANTITY", "100000")),
            max_order_value_rupees=int(env.get("MAX_ORDER_VALUE_RUPEES", "10000000")),
        )
