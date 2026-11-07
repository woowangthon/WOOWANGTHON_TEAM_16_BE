"""장치가 부르는 API: 노드의 CSI 프레임, 화재 수신기의 경보."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import building_from_alarm_token, node_from_token
from ..ml.csi import CsiFormatError, Frame, iq_to_amplitude
from ..models import Building, CalibrationRun, Event, Incident, Node, utcnow
from ..schemas import AlarmIn, IngestIn
from ..services.stream import hub

router = APIRouter(prefix="/api", tags=["devices"])


@router.post("/ingest/csi")
def ingest_csi(body: IngestIn, node: Node = Depends(node_from_token), db: Session = Depends(get_db)):
    frames: list[Frame] = []
    bad = 0
    for item in body.frames:
        try:
            amp = iq_to_amplitude(item.data)
        except CsiFormatError:
            bad += 1
            continue
        frames.append(Frame(item.t, float("nan") if item.rssi is None else float(item.rssi), amp))
    result = hub.ingest(db, node, frames, source=body.source, restarted=body.restarted)
    result["rejected"] = bad
    return result


def open_incident(db: Session, building: Building, source: str, note: str) -> tuple[Incident, bool]:
    """열린 상황이 있으면 그대로 돌려주고, 없으면 새로 연다. 진행 중인 보정은 중단한다."""
    current = hub.active_incident(db, building.id)
    if current is not None:
        return current, False
    for run in db.scalars(select(CalibrationRun).where(CalibrationRun.building_id == building.id,
                                                       CalibrationRun.status == "running")):
        run.status = "failed"
        run.finished_at = utcnow()
        run.result = {"_note": "화재 상황이 열려 보정을 중단함"}
    incident = Incident(building_id=building.id, source=source, note=note)
    db.add(incident)
    db.flush()
    label = {"alarm": "화재 경보 수신", "manual": "지휘관 수동 개시", "measurement": "현장 실측", "replay": "녹화 데이터 재생 시작"}.get(source, source)
    db.add(Event(building_id=building.id, incident_id=incident.id, kind="incident_open",
                 message=f"{label} — 감지를 시작합니다." + (f" ({note})" if note else "")))
    db.commit()
    return incident, True


@router.post("/alarm")
def fire_alarm(body: AlarmIn, building: Building = Depends(building_from_alarm_token), db: Session = Depends(get_db)):
    note = "시험 경보" if body.event == "test" else body.detail
    incident, created = open_incident(db, building, "alarm", note)
    return {"incident_id": incident.id, "created": created}
