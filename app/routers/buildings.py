"""건물·평면도·구역·노드·기준선 보정."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, undefer

from ..db import get_db
from ..deps import current_user, get_building_or_404
from ..models import Baseline as BaselineRow
from ..models import Building, CalibrationRun, Event, Node, User, Zone
from ..schemas import BuildingIn, BuildingPatch, CalibrationStartIn, NodeIn, NodePatch, ZoneIn, ZonePatch
from ..security import new_device_token, token_digest
from ..serializers import building_out, calibration_out, incident_out, node_out, zone_out
from ..services.stream import CalibrationError, hub

router = APIRouter(prefix="/api", tags=["buildings"])

MAX_PLAN_BYTES = 4 * 1024 * 1024


def _sniff_image(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _building_detail(db: Session, b: Building) -> dict:
    # 보정 시간이 지났으면 여기서 마무리되므로, 기준선 여부보다 먼저 확인한다
    run = hub.active_calibration(db, b.id)
    incident = hub.active_incident(db, b.id)
    nodes = db.scalars(select(Node).where(Node.building_id == b.id).order_by(Node.id)).all()
    zones = db.scalars(select(Zone).where(Zone.building_id == b.id).order_by(Zone.id)).all()
    calibrated = set()
    if nodes:
        calibrated = set(db.scalars(select(BaselineRow.node_id).where(
            BaselineRow.node_id.in_([n.id for n in nodes]), BaselineRow.active.is_(True))).all())
    if run is None:
        run = db.scalar(select(CalibrationRun).where(CalibrationRun.building_id == b.id)
                        .order_by(CalibrationRun.id.desc()))
    progress = None
    if run is not None and run.status == "running":
        progress = {str(k): v for k, v in hub.calibration_progress([n.id for n in nodes if n.role == "rx"]).items()}
    out = building_out(b, zones=len(zones), nodes=len(nodes),
                       active_incident_id=incident.id if incident else None)
    out["zones"] = [zone_out(z) for z in zones]
    out["nodes"] = [node_out(n, calibrated=(n.id in calibrated) if n.role == "rx" else None) for n in nodes]
    out["calibration"] = calibration_out(run, progress)
    out["active_incident"] = incident_out(incident) if incident else None
    return out


@router.get("/buildings")
def list_buildings(db: Session = Depends(get_db), _: User = Depends(current_user)):
    rows = db.scalars(select(Building).order_by(Building.id)).all()
    zone_counts = dict(db.execute(select(Zone.building_id, func.count()).group_by(Zone.building_id)).all())
    node_counts = dict(db.execute(select(Node.building_id, func.count()).group_by(Node.building_id)).all())
    out = []
    for b in rows:
        inc = hub.active_incident(db, b.id)
        out.append(building_out(b, zones=zone_counts.get(b.id, 0), nodes=node_counts.get(b.id, 0),
                                active_incident_id=inc.id if inc else None))
    return out


@router.post("/buildings", status_code=201)
def create_building(body: BuildingIn, db: Session = Depends(get_db), _: User = Depends(current_user)):
    token = new_device_token()
    b = Building(name=body.name.strip(), address=body.address.strip(), floor_label=body.floor_label.strip(),
                 alarm_token_hash=token_digest(token))
    db.add(b)
    db.commit()
    out = _building_detail(db, b)
    out["alarm_token"] = token  # 이번 응답에서만 보여 준다
    return out


@router.get("/buildings/{building_id}")
def get_building(building_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return _building_detail(db, get_building_or_404(db, building_id))


@router.patch("/buildings/{building_id}")
def patch_building(building_id: int, body: BuildingPatch, db: Session = Depends(get_db),
                   _: User = Depends(current_user)):
    b = get_building_or_404(db, building_id)
    for field in ("name", "address", "floor_label"):
        value = getattr(body, field)
        if value is not None:
            setattr(b, field, value.strip())
    db.commit()
    return _building_detail(db, b)


@router.delete("/buildings/{building_id}", status_code=204)
def delete_building(building_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    b = get_building_or_404(db, building_id)
    if hub.active_incident(db, b.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "화재 상황이 열려 있는 건물은 삭제할 수 없습니다.")
    db.delete(b)
    db.commit()
    return Response(status_code=204)


@router.post("/buildings/{building_id}/alarm-token")
def rotate_alarm_token(building_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    b = get_building_or_404(db, building_id)
    token = new_device_token()
    b.alarm_token_hash = token_digest(token)
    db.add(Event(building_id=b.id, kind="config", message="경보 토큰을 새로 발급했습니다."))
    db.commit()
    return {"alarm_token": token}


# ---- 평면도 ----------------------------------------------------------
@router.put("/buildings/{building_id}/plan")
async def upload_plan(
    building_id: int,
    file: UploadFile = File(...),
    width: int = Form(..., ge=50, le=10000),
    height: int = Form(..., ge=50, le=10000),
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
):
    b = get_building_or_404(db, building_id)
    data = await file.read(MAX_PLAN_BYTES + 1)
    if len(data) > MAX_PLAN_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "평면도 이미지는 4MB 이하여야 합니다.")
    mime = _sniff_image(data)
    if mime is None:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "PNG, JPEG, WebP 이미지만 올릴 수 있습니다.")
    b.plan_image = data
    b.plan_mime = mime
    b.plan_width = width
    b.plan_height = height
    db.commit()
    return _building_detail(db, b)


@router.get("/buildings/{building_id}/plan")
def get_plan(building_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    b = db.scalar(select(Building).where(Building.id == building_id).options(undefer(Building.plan_image)))
    if not b or not b.plan_image:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "평면도가 없습니다.")
    return Response(content=b.plan_image, media_type=b.plan_mime or "application/octet-stream",
                    headers={"Cache-Control": "private, max-age=60"})


@router.delete("/buildings/{building_id}/plan")
def delete_plan(building_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    b = get_building_or_404(db, building_id)
    b.plan_image = None
    b.plan_mime = None
    b.plan_width = None
    b.plan_height = None
    db.commit()
    return _building_detail(db, b)


# ---- 구역 ------------------------------------------------------------
@router.post("/buildings/{building_id}/zones", status_code=201)
def create_zone(building_id: int, body: ZoneIn, db: Session = Depends(get_db), _: User = Depends(current_user)):
    b = get_building_or_404(db, building_id)
    z = Zone(building_id=b.id, name=body.name.strip(), polygon=body.polygon)
    db.add(z)
    db.commit()
    return zone_out(z)


def _zone_or_404(db: Session, zone_id: int) -> Zone:
    z = db.get(Zone, zone_id)
    if not z:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "구역을 찾을 수 없습니다.")
    return z


@router.patch("/zones/{zone_id}")
def patch_zone(zone_id: int, body: ZonePatch, db: Session = Depends(get_db), _: User = Depends(current_user)):
    z = _zone_or_404(db, zone_id)
    if body.name is not None:
        z.name = body.name.strip()
    if body.polygon is not None:
        z.polygon = body.polygon
    db.commit()
    return zone_out(z)


@router.delete("/zones/{zone_id}", status_code=204)
def delete_zone(zone_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    z = _zone_or_404(db, zone_id)
    for n in db.scalars(select(Node).where(Node.zone_id == z.id)):
        n.zone_id = None
    db.delete(z)
    db.commit()
    return Response(status_code=204)


# ---- 노드 ------------------------------------------------------------
def _check_zone(db: Session, building_id: int, zone_id: int | None) -> None:
    if zone_id is None:
        return
    z = db.get(Zone, zone_id)
    if not z or z.building_id != building_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "같은 건물의 구역만 연결할 수 있습니다.")


@router.post("/buildings/{building_id}/nodes", status_code=201)
def create_node(building_id: int, body: NodeIn, db: Session = Depends(get_db), _: User = Depends(current_user)):
    b = get_building_or_404(db, building_id)
    _check_zone(db, b.id, body.zone_id)
    token = new_device_token() if body.role == "rx" else None
    n = Node(building_id=b.id, name=body.name.strip(), role=body.role, x=body.x, y=body.y,
             zone_id=body.zone_id, token_hash=token_digest(token) if token else None)
    db.add(n)
    db.commit()
    out = node_out(n, calibrated=False if n.role == "rx" else None)
    if token:
        out["token"] = token  # 이번 응답에서만 보여 준다
    return out


def _node_or_404(db: Session, node_id: int) -> Node:
    n = db.get(Node, node_id)
    if not n:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "노드를 찾을 수 없습니다.")
    return n


@router.patch("/nodes/{node_id}")
def patch_node(node_id: int, body: NodePatch, db: Session = Depends(get_db), _: User = Depends(current_user)):
    n = _node_or_404(db, node_id)
    if body.name is not None:
        n.name = body.name.strip()
    if body.x is not None:
        n.x = body.x
    if body.y is not None:
        n.y = body.y
    if body.clear_zone:
        n.zone_id = None
    elif body.zone_id is not None:
        _check_zone(db, n.building_id, body.zone_id)
        n.zone_id = body.zone_id
    db.commit()
    return node_out(n)


@router.post("/nodes/{node_id}/token")
def rotate_node_token(node_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    n = _node_or_404(db, node_id)
    if n.role != "rx":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "송신 노드는 토큰이 필요 없습니다.")
    token = new_device_token()
    n.token_hash = token_digest(token)
    db.commit()
    return {"token": token}


@router.delete("/nodes/{node_id}", status_code=204)
def delete_node(node_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    n = _node_or_404(db, node_id)
    db.delete(n)
    db.commit()
    return Response(status_code=204)


# ---- 기준선 보정 -----------------------------------------------------
@router.post("/buildings/{building_id}/calibration/start")
def start_calibration(building_id: int, body: CalibrationStartIn, db: Session = Depends(get_db),
                      _: User = Depends(current_user)):
    b = get_building_or_404(db, building_id)
    try:
        hub.start_calibration(db, b.id, body.duration_seconds)
    except CalibrationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _building_detail(db, b)


@router.post("/buildings/{building_id}/calibration/stop")
def stop_calibration(building_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    b = get_building_or_404(db, building_id)
    run = db.scalar(select(CalibrationRun).where(CalibrationRun.building_id == b.id,
                                                 CalibrationRun.status == "running")
                    .order_by(CalibrationRun.id.desc()))
    if run is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "진행 중인 보정이 없습니다.")
    hub.finish_calibration(db, run)
    return _building_detail(db, b)
