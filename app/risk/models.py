"""The trader's own settings. Money is integer paise; percentages are 1 through 100."""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import Field, StrictBool, model_validator

from app.schemas import Model, Product

PresetName = Literal["conservative", "balanced", "aggressive"]
PositiveCount = Annotated[int, Field(strict=True, gt=0, le=100_000)]
Percentage = Annotated[float, Field(strict=True, ge=1, le=100, allow_inf_nan=False)]
Money = Annotated[int, Field(strict=True, gt=0, le=9_007_199_254_740_991)]

STARTING_VALUES_NOTE = "Starting values; change them to your own. These are not recommendations."


class RiskProfile(Model):
    style: Literal["conservative", "balanced", "aggressive", "custom"]
    max_orders_per_day: PositiveCount
    max_order_pct: Percentage
    max_stock_pct: Percentage
    daily_loss_limit_pct: Percentage
    cooling_off_after_losses: PositiveCount
    cooling_off_minutes: Annotated[int, Field(strict=True, ge=1, le=1440)]
    reentry_minutes: Annotated[int, Field(strict=True, ge=1, le=1440)]
    intraday_allowed: StrictBool
    hard_order_limit: StrictBool = False
    hard_stop_on_daily_loss: StrictBool = False
    hide_day_pnl: StrictBool = False


class PresetOption(Model):
    name: PresetName
    profile: RiskProfile
    note: str = STARTING_VALUES_NOTE


class OnboardingAnswers(Model):
    daily_loss_comfort: Literal["small", "moderate", "larger"]
    holding_period: Literal["weeks_or_more", "days", "same_day"]
    usual_orders: Literal["up_to_three", "four_to_six", "seven_or_more"]
    intraday_allowed: StrictBool
    aim: Literal["preserve_capital", "steady_progress", "active_trading"]


class OnboardingSuggestion(Model):
    preset: PresetName
    profile: RiskProfile
    explanation: str
    note: str = STARTING_VALUES_NOTE
    requires_review: Literal[True] = True


class GoalRequest(Model):
    """User-entered goal. The server captures start_value; callers cannot supply it.

    Amount fields use paise to match the existing API. Convert rupees at the UI boundary.
    start_date may be omitted; when provided it must be today's Indian trading date.
    """

    target_paise: Money | None = None
    target_pct: Percentage | None = None
    start_date: date | None = None
    end_date: date
    max_acceptable_loss_paise: Money

    @model_validator(mode="after")
    def _one_target_and_date_order(self):
        if (self.target_paise is None) == (self.target_pct is None):
            raise ValueError("Set exactly one of target_paise or target_pct.")
        if self.start_date is not None and self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date.")
        return self


class Goal(GoalRequest):
    start_date: date
    start_value: Money = Field(description="Portfolio value in paise, captured by the backend when saved.")


class ClosedTrade(Model):
    order_id: str
    instrument_key: str
    product: Product
    quantity: int
    pnl: int
    closed_at: datetime


class TodayFacts(Model):
    day: date
    orders_today: int
    turnover: int
    charges: int
    realised_pnl: int
    unrealised_pnl: int
    pnl_estimate: int
    pnl_after_charges: int
    portfolio_value: int
    largest_order_pct: float
    largest_stock_pct: float
    stock_values: dict[str, int]
    intraday_share_pct: float
    consecutive_losses: int
    last_loss_at: datetime | None
    last_loss_by_stock: dict[str, datetime]
    reentries: int
    cooling_off_breaches: int
    closed_trades: list[ClosedTrade]
    notes: list[str]


class ScoreComponent(Model):
    key: Literal["activity", "size", "concentration", "intraday", "loss_chasing"]
    label: str
    score: Annotated[float, Field(ge=0, le=100)]
    weight: int


class RiskScore(Model):
    total: Annotated[int, Field(ge=0, le=100)]
    components: list[ScoreComponent]


class DisciplineDay(Model):
    day: date
    orders: int
    turnover: int
    pnl: int
    charges: int
    pnl_after_charges: int
    risk_score: int | None
    components: list[ScoreComponent]
    demo: bool


class ComparisonGroup(Model):
    days: int
    net_pnl: int
    profitable_days: int


class HistoryComparison(Model):
    average_score: float | None
    baseline_days: int
    source: Literal["real", "demo", "none"]
    above_usual: ComparisonGroup
    at_or_below_usual: ComparisonGroup
    days: list[DisciplineDay]
    note: str = "Past days don't predict future ones."


class GoalProgress(Model):
    goal: Goal
    progress_paise: int
    target_paise: int
    progress_pct: float
    days_left: int
    remaining_paise: int
    needed_per_week_paise: int | None
    pace_paise: int
    pace_text: str
    loss_headroom_paise: int
    status: Literal["not_started", "active", "expired", "achieved"]
    note: str = "Arithmetic against your chosen goal, not a forecast. Deposits and withdrawals affect portfolio value."


class ChargesMeter(Model):
    today_paise: int
    turnover_pct: float
    last_30_days_paise: int
    last_30_days_demo_paise: int


class DisciplineReport(Model):
    profile: RiskProfile | None
    today: TodayFacts
    score: RiskScore | None
    history: HistoryComparison
    goal: GoalProgress | None
    charges: ChargesMeter
    warnings: list[str]


class DemoSeedResult(Model):
    seeded_days: int
