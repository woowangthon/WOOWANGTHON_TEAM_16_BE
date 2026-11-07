from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, get_building_or_404
from ..models import Decision, Event, Incident, Inference, User, Zone, utcnow
from ..schemas import DecisionIn, IncidentIn
from ..serializers import incident_out, iso
from ..services.state import incident_state
from ..services.stream import as_aware, hub
from .ingest import open_incident

router = APIRouter(prefix="/api", tags=["incidents"])

ACTION_KO = {
    "searching": "수색 중",
    "rescued": "구조 완료",
    "cleared": "수색 완료(발견 없음)",
    "blocked": "진입 불가",
    "note": "메모",
}


def _incident_or_404(db: Session, incident_id: int) -> Incident:
    inc = db.get(Incident, incident_id)
    if not inc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "상황을 찾을 수 없습니다.")
    return inc


@router.post("/buildings/{building_id}/incidents", status_code=201)
def open_manual(building_id: int, body: IncidentIn, db: Session = Depends(get_db), _: User = Depends(current_user)):
    b = get_building_or_404(db, building_id)
    incident, created = open_incident(db, b, "measurement" if body.measurement else "manual", body.note.strip())
    out = incident_out(incident)
    out["created"] = created
    return out


@router.get("/buildings/{building_id}/incidents")
def list_incidents(building_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    get_building_or_404(db, building_id)
    rows = db.scalars(select(Incident).where(Incident.building_id == building_id)
                      .order_by(Incident.id.desc()).limit(30)).all()
    return [incident_out(i) for i in rows]


@router.get("/incidents/{incident_id}")
def get_incident(incident_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return incident_out(_incident_or_404(db, incident_id))


@router.post("/incidents/{incident_id}/close")
def close_incident(incident_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    inc = _incident_or_404(db, incident_id)
    if inc.status != "active":
        return incident_out(inc)
    inc.status = "closed"
    inc.ended_at = utcnow()
    db.add(Event(building_id=inc.building_id, incident_id=inc.id, kind="incident_close",
                 message=f"상황 종료({user.username}). 감지를 멈춥니다."))
    db.commit()
    hub.forget_incident(inc.id)
    return incident_out(inc)


@router.get("/incidents/{incident_id}/state")
def get_state(incident_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return incident_state(db, _incident_or_404(db, incident_id))


@router.post("/incidents/{incident_id}/decisions", status_code=201)
def add_decision(incident_id: int, body: DecisionIn, db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    inc = _incident_or_404(db, incident_id)
    if inc.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "종료된 상황에는 기록을 추가할 수 없습니다.")
    zone = db.get(Zone, body.zone_id)
    if not zone or zone.building_id != inc.building_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "이 건물의 구역이 아닙니다.")
    if body.action == "rescued" and not body.people_count:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "구조 완료는 구조 인원을 1명 이상 입력해야 합니다.")
    if body.action == "note" and not body.note.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "메모 내용을 입력하세요.")
    d = Decision(incident_id=inc.id, zone_id=zone.id, action=body.action,
                 people_count=body.people_count, note=body.note.strip(), username=user.username)
    db.add(d)
    db.commit()
    return {"id": d.id, "zone_id": zone.id, "action": d.action, "action_ko": ACTION_KO[d.action],
            "people_count": d.people_count, "note": d.note, "at": iso(d.created_at)}


@router.get("/incidents/{incident_id}/report")
def report(incident_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    inc = _incident_or_404(db, incident_id)
    state = incident_state(db, inc)
    windows = db.scalar(select(func.count()).select_from(Inference).where(Inference.incident_id == inc.id)) or 0
    start = as_aware(inc.started_at)
    end = as_aware(inc.ended_at) if inc.ended_at else utcnow()
    decisions = db.scalars(select(Decision).where(Decision.incident_id == inc.id).order_by(Decision.id)).all()
    zone_names = {z["id"]: z["name"] for z in state["zones"]}
    events = db.scalars(select(Event).where(Event.incident_id == inc.id).order_by(Event.id)).all()
    return {
        "incident": incident_out(inc),
        "building": state["building"],
        "mode": state["mode"],
        "duration_seconds": max(0, int((end - start).total_seconds())),
        "windows_analyzed": windows,
        "zones": state["zones"],
        "decisions": [{"t": iso(d.created_at), "zone": zone_names.get(d.zone_id), "action": d.action,
                       "action_ko": ACTION_KO[d.action], "people_count": d.people_count, "note": d.note,
                       "username": d.username} for d in decisions],
        "events": [{"t": iso(e.created_at), "kind": e.kind, "message": e.message} for e in events],
        "rescued_people": state["summary"]["rescued_people"],
    }
