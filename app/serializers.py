from __future__ import annotations

from datetime import datetime, timezone

from .models import Building, CalibrationRun, Incident, Node, Zone


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def building_out(b: Building, *, zones: int | None = None, nodes: int | None = None,
                 active_incident_id: int | None = None) -> dict:
    return {
        "id": b.id,
        "name": b.name,
        "address": b.address,
        "floor_label": b.floor_label,
        "has_plan": b.plan_mime is not None,
        "plan_width": b.plan_width,
        "plan_height": b.plan_height,
        "has_alarm_token": b.alarm_token_hash is not None,
        "is_demo": b.is_demo,
        "created_at": iso(b.created_at),
        "zone_count": zones,
        "node_count": nodes,
        "active_incident_id": active_incident_id,
    }


def zone_out(z: Zone) -> dict:
    return {"id": z.id, "name": z.name, "polygon": z.polygon}


def node_out(n: Node, *, calibrated: bool | None = None, restarted: bool = False) -> dict:
    """restarted: 보정한 뒤 다시 켜져서 기준선을 믿을 수 없음(calibrated도 False로 내보낸다)."""
    return {
        "id": n.id,
        "name": n.name,
        "role": n.role,
        "x": n.x,
        "y": n.y,
        "zone_id": n.zone_id,
        "has_token": n.token_hash is not None,
        "last_seen_at": iso(n.last_seen_at),
        "last_rssi": n.last_rssi,
        "calibrated": False if restarted else calibrated,
        "restarted_since_calibration": restarted,
    }


def incident_out(i: Incident) -> dict:
    return {
        "id": i.id,
        "building_id": i.building_id,
        "source": i.source,
        "status": i.status,
        "started_at": iso(i.started_at),
        "ended_at": iso(i.ended_at),
        "note": i.note,
    }


def calibration_out(run: CalibrationRun | None, progress: dict | None = None) -> dict | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "status": run.status,
        "started_at": iso(run.started_at),
        "ends_at": iso(run.ends_at),
        "finished_at": iso(run.finished_at),
        "result": run.result,
        "progress": progress,
    }
