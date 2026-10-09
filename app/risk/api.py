"""Explicit trader settings only. This router cannot place, modify or cancel an order."""

from fastapi import APIRouter, HTTPException, Request, Response

from app.history.store import trading_day
from app.risk.models import (DemoSeedResult, DisciplineReport, Goal, GoalRequest, OnboardingAnswers,
                             OnboardingSuggestion, PresetOption, RiskProfile)
from app.risk.presets import all_presets, suggest_profile
from app.risk.store import ProfileStore
from app.risk.valuation import portfolio_value

router = APIRouter(prefix="/api", tags=["Discipline"])


def profile_store(request: Request) -> ProfileStore:
    return request.app.state.profile_store


@router.get("/profile", response_model=RiskProfile | None)
async def get_profile(request: Request):
    return profile_store(request).get_profile()


@router.put("/profile", response_model=RiskProfile)
async def put_profile(body: RiskProfile, request: Request):
    saved = profile_store(request).save_profile(body)
    await request.app.state.discipline.refresh_safely()
    return saved


@router.get("/profile/presets", response_model=list[PresetOption])
async def get_presets():
    return all_presets()


@router.post("/profile/onboarding", response_model=OnboardingSuggestion)
async def onboarding(body: OnboardingAnswers):
    return suggest_profile(body)


@router.get("/goal", response_model=Goal | None)
async def get_goal(request: Request):
    return profile_store(request).get_goal()


@router.put("/goal", response_model=Goal)
async def put_goal(body: GoalRequest, request: Request):
    today = trading_day(request.app.state.clock())
    if body.start_date is not None and body.start_date != today:
        raise HTTPException(422, "A new goal starts today; a historical portfolio value cannot be reconstructed.")
    if body.end_date <= today:
        raise HTTPException(422, "The goal end date must be after today.")
    value = await portfolio_value(request.app.state.broker.read_only())
    if value <= 0:
        raise HTTPException(409, "A positive portfolio value is needed to start a goal.")
    if body.max_acceptable_loss_paise > value:
        raise HTTPException(422, "Maximum acceptable loss cannot exceed the starting portfolio value.")
    goal = Goal(**{**body.model_dump(), "start_date": today, "start_value": value})
    return profile_store(request).save_goal(goal)


@router.delete("/goal", status_code=204)
async def delete_goal(request: Request):
    profile_store(request).delete_goal()
    return Response(status_code=204)


@router.get("/discipline", response_model=DisciplineReport)
async def get_discipline(request: Request):
    return await request.app.state.discipline.refresh()


@router.post("/discipline/demo-seed", response_model=DemoSeedResult)
async def seed_demo(request: Request):
    if not request.app.state.settings.demo_mode:
        raise HTTPException(404, "Not found")
    count = await request.app.state.discipline.seed()
    await request.app.state.discipline.refresh_safely()
    return DemoSeedResult(seeded_days=count)


@router.delete("/discipline/demo-seed", status_code=204)
async def clear_demo(request: Request):
    if not request.app.state.settings.demo_mode:
        raise HTTPException(404, "Not found")
    await request.app.state.discipline.clear_demo()
    await request.app.state.discipline.refresh_safely()
    return Response(status_code=204)
