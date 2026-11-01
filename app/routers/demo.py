from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from ..deps import current_user
from ..models import User
from ..schemas import ReplayStartIn
from ..services.replay import ReplayError, load_scenarios, runner

router = APIRouter(prefix="/api/demo", tags=["demo"])


@router.get("/scenarios")
def scenarios(_: User = Depends(current_user)):
    return [s.public() for s in load_scenarios()]


@router.post("/scenarios/{scenario_id}/start")
def start(scenario_id: str, body: ReplayStartIn, _: User = Depends(current_user)):
    try:
        return runner.start(scenario_id, body.speed)
    except ReplayError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/stop")
def stop(_: User = Depends(current_user)):
    return runner.stop()


@router.get("/status")
def replay_status(_: User = Depends(current_user)):
    return runner.state
