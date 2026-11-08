"""장치가 부르는 API: 노드의 CSI 프레임, 화재 수신기의 경보."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
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
    """상황을 열거나 실제 경보로 승격한다. 새 상황은 진행 중인 보정을 중단한다."""
    # 잠금 밖에서 계산한 보정 결과가 경보 개시 뒤에 반영되지 않게 한다.
    with hub.lifecycle_lock():
        return _open_incident_locked(db, building, source, note)


def _open_incident_locked(db: Session, building: Building, source: str, note: str) -> tuple[Incident, bool]:
    current = hub.active_incident(db, building.id)
    if current is not None:
        db.refresh(current)  # 다른 요청이 이미 승격했을 수 있으므로 최신 모드를 읽는다.
    if source == "alarm" and (building.is_demo or (current is not None and current.source == "replay")):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "녹화 시연에는 실제 화재 경보를 연결할 수 없습니다. 실제 건물에서 경보를 시작하세요.")
    if current is not None:
        if source == "alarm":
            previous = current.source
            if previous in {"measurement", "manual"}:
                current.source = "alarm"
                db.flush()  # autoflush=False: 이후 판정과 기록이 승격된 모드를 보게 한다.
                db.add(Event(building_id=building.id, incident_id=current.id, kind="mode_changed",
                             message=f"{previous} → alarm: 실제 화재 경보를 수신해 상황을 전환했습니다."))
            _record_alarm(db, building.id, current.id, note)
            if previous == "measurement":
                # 실측의 낮은 문턱으로 얻은 판단은 실제 경보의 첫 감지로 재사용하지 않는다.
                hub.reset_detection(db, current)
            db.commit()
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
    if source == "alarm":
        _record_alarm(db, building.id, incident.id, note)
    db.commit()
    return incident, True


def _record_alarm(db: Session, building_id: int, incident_id: int, detail: str) -> None:
    db.add(Event(building_id=building_id, incident_id=incident_id, kind="alarm_received",
                 message="실제 화재 경보 수신" + (f": {detail}" if detail else "")))


@router.post("/alarm")
def fire_alarm(body: AlarmIn, building: Building = Depends(building_from_alarm_token), db: Session = Depends(get_db)):
    with hub.lifecycle_lock():
        if body.event == "test":
            # 시험은 실제 화재 승격과 구별하고, 상황이 없으면 수동 훈련으로 연다.
            note = "시험 경보" + (f": {body.detail}" if body.detail else "")
            incident, created = _open_incident_locked(db, building, "manual", note)
            db.add(Event(building_id=building.id, incident_id=incident.id, kind="alarm_test", message=note))
            db.commit()
        else:
            incident, created = _open_incident_locked(db, building, "alarm", body.detail)
        return {"incident_id": incident.id, "created": created}
