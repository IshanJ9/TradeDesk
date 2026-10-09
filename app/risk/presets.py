"""Transparent starting values and a deterministic onboarding mapping, not financial advice."""

from app.risk.models import OnboardingAnswers, OnboardingSuggestion, PresetName, PresetOption, RiskProfile

_VALUES = {
    "conservative": (3, 10, 20, 1, 2, 30, 60, False),
    "balanced": (6, 20, 30, 2, 3, 20, 30, True),
    "aggressive": (12, 35, 45, 4, 4, 10, 15, True),
}


def preset(name: PresetName) -> RiskProfile:
    orders, order_pct, stock_pct, loss_pct, losses, cooling, reentry, intraday = _VALUES[name]
    return RiskProfile(
        style=name, max_orders_per_day=orders, max_order_pct=order_pct,
        max_stock_pct=stock_pct, daily_loss_limit_pct=loss_pct,
        cooling_off_after_losses=losses, cooling_off_minutes=cooling,
        reentry_minutes=reentry, intraday_allowed=intraday,
    )


def all_presets() -> list[PresetOption]:
    return [PresetOption(name=name, profile=preset(name)) for name in _VALUES]


def suggest_profile(answers: OnboardingAnswers) -> OnboardingSuggestion:
    """Each of five answers contributes 0, 1 or 2. No model makes this decision.

    Totals 0-3 select conservative, 4-6 balanced, 7-10 aggressive. The trader's explicit
    intraday answer always wins over the preset. A suggestion is never saved automatically.
    """
    score = sum((
        {"small": 0, "moderate": 1, "larger": 2}[answers.daily_loss_comfort],
        {"weeks_or_more": 0, "days": 1, "same_day": 2}[answers.holding_period],
        {"up_to_three": 0, "four_to_six": 1, "seven_or_more": 2}[answers.usual_orders],
        2 if answers.intraday_allowed else 0,
        {"preserve_capital": 0, "steady_progress": 1, "active_trading": 2}[answers.aim],
    ))
    name: PresetName = "conservative" if score <= 3 else "balanced" if score <= 6 else "aggressive"
    profile = preset(name).model_copy(update={"intraday_allowed": answers.intraday_allowed})
    return OnboardingSuggestion(
        preset=name, profile=profile,
        explanation=(f"Your five answers total {score} on this questionnaire's 0-10 scale, "
                     f"which selects the {name} starting values. Your intraday choice is preserved. "
                     "Review and edit every limit before saving; nothing has been applied."),
    )
