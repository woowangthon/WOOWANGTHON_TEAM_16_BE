"""지휘 대시보드에 보낼 구역 상태를 만든다.

노드별 최근 창 확률을 지수평활하고, 구역 안에서 신호가 정상인 노드 중 가장 높은 값을 구역 확률로 쓴다.
'미감지'는 부재를 뜻하지 않으므로 우선순위에서 '판단 불가'보다 아래일 뿐, '없음'으로 표시하지 않는다.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..ml.features import MIN_WINDOW_QUALITY, evidence_from_z
from ..models import Baseline as BaselineRow
from ..models import Building, Decision, Event, Incident, Node, Zone, utcnow
from ..serializers import incident_out, iso
from .registry import registry
from .stream import as_aware, detection_thresholds, hub, recent_probabilities, smoothed_probability

MIN_WINDOWS_FOR_NOT_DETECTED = 3

STATE_RANK = {
    "detected": 0,
    "possible": 1,
    "no_signal": 2,
    "warming": 3,
    "uncalibrated": 4,
    "not_detected": 5,
    "no_sensor": 6,
}
HANDLED_ACTIONS = {"rescued", "cleared"}


def _node_infos(db: Session, incident: Incident, rx: list[Node], freeze: bool = False) -> dict[int, dict]:
    """freeze=True면 마지막 결과를 그대로 보여 준다(종료된 상황, 재생이 끝난 시연)."""
    now = utcnow()
    ids = [n.id for n in rx]
    calibrated = set()
    if ids:
        calibrated = set(db.scalars(select(BaselineRow.node_id).where(
            BaselineRow.node_id.in_(ids), BaselineRow.active.is_(True))).all())
    # 종료 뒤 재시작은 당시의 마지막 판단을 바꾸지 않는다.
    restart_cutoff = incident.ended_at if incident.status != "active" else None
    restarted = {n.id for n in rx if n.id in calibrated
                 and hub.restarted_since_baseline(db, n.id, cutoff=restart_cutoff)}
    out: dict[int, dict] = {}
    for n in rx:
        rows = recent_probabilities(db, incident.id, n.id)
        # 프레임이 모자란 창은 특징이 사람 없이도 커질 수 있으므로 확률 계산에 넣지 않는다
        good = [r for r in rows if r.quality is None or r.quality >= MIN_WINDOW_QUALITY]
        low_rate = bool(rows) and rows[-1].quality is not None and rows[-1].quality < MIN_WINDOW_QUALITY
        ema = smoothed_probability(rows)
        age = None
        if n.last_seen_at is not None:
            age = (now - as_aware(n.last_seen_at)).total_seconds()
        reason = None
        if n.id not in calibrated:
            status = "uncalibrated"
        elif n.id in restarted:
            status, reason = "stale", "restarted"  # 신호는 오지만 기준선과 크기 기준이 어긋났을 수 있음
        elif freeze and good:
            status = "ok"
        elif age is None or age > settings.stale_seconds:
            status = "stale"
        elif not rows:
            status = "warming"
        elif low_rate:
            status, reason = "stale", "low_rate"  # 신호는 오지만 프레임이 모자라 판단할 수 없음
        elif not good:
            status = "warming"
        elif (now - as_aware(good[-1].t)).total_seconds() > settings.stale_seconds:
            # 접속 시각만 보면 깨진 프레임·중복 프레임만 오는 동안에도 오래된 판단을 정상으로 보인다.
            status, reason = "stale", "stale_inference"
        else:
            status = "ok"
        last = good[-1] if good else None
        out[n.id] = {
            "id": n.id,
            "name": n.name,
            "role": "rx",
            "zone_id": n.zone_id,
            "x": n.x,
            "y": n.y,
            "status": status,
            "reason": reason,
            "p": None if ema is None else round(ema, 3),
            "windows": len(good),
            "last_seen_s": None if age is None else round(age, 1),
            "quality": rows[-1].quality if rows else None,
            "evidence": [] if last is None else evidence_from_z(last.z),
            "source": None if last is None else last.source,
        }
    return out


def _zone_state(infos: list[dict], source: str = "manual") -> tuple[str, float | None, list[str], bool]:
    if not infos:
        return "no_sensor", None, [], False
    ok = [i for i in infos if i["status"] == "ok" and i["p"] is not None]
    if ok:
        best = max(ok, key=lambda i: i["p"])
        p = best["p"]
        detect, possible = detection_thresholds(source, registry.model is not None)
        if p >= detect:
            state = "detected"
        elif p >= possible:
            state = "possible"
        elif max(i["windows"] for i in ok) >= MIN_WINDOWS_FOR_NOT_DETECTED:
            state = "not_detected"
        else:
            state = "warming"
        return state, p, best["evidence"], len(ok) < len(infos)
    statuses = {i["status"] for i in infos}
    if "warming" in statuses:
        return "warming", None, [], False
    if "stale" in statuses:
        return "no_signal", None, [], False
    return "uncalibrated", None, [], False


def incident_state(db: Session, incident: Incident) -> dict:
    building = db.get(Building, incident.building_id)
    zones = db.scalars(select(Zone).where(Zone.building_id == building.id).order_by(Zone.id)).all()
    nodes = db.scalars(select(Node).where(Node.building_id == building.id).order_by(Node.id)).all()
    rx = [n for n in nodes if n.role == "rx"]
    from .replay import runner  # 순환 import 방지

    replay_finished = incident.source == "replay" and not runner.is_running(incident.id)
    freeze = incident.status != "active" or replay_finished
    infos = _node_infos(db, incident, rx, freeze=freeze)

    decisions = db.scalars(select(Decision).where(Decision.incident_id == incident.id).order_by(Decision.id)).all()
    latest: dict[int, Decision] = {}
    for d in decisions:
        if d.action != "note":
            latest[d.zone_id] = d
    first_detect = {
        e.zone_id: e.created_at
        for e in db.scalars(select(Event).where(
            Event.incident_id == incident.id, Event.kind == "first_detection"))
    }

    zone_rows = []
    for z in zones:
        zinfos = [infos[n.id] for n in rx if n.zone_id == z.id]
        state, p, evidence, partial = _zone_state(zinfos, incident.source)
        d = latest.get(z.id)
        zone_rows.append({
            "id": z.id,
            "name": z.name,
            "polygon": z.polygon,
            "state": state,
            "p": p,
            "evidence": evidence,
            "partial_signal": partial,
            "node_ids": [i["id"] for i in zinfos],
            "detected_since": iso(first_detect.get(z.id)),
            "decision": None if d is None else {
                "action": d.action,
                "people_count": d.people_count,
                "note": d.note,
                "username": d.username,
                "at": iso(d.created_at),
            },
        })

    def sort_key(z):
        handled = 1 if z["decision"] and z["decision"]["action"] in HANDLED_ACTIONS else 0
        return handled, STATE_RANK[z["state"]], -(z["p"] or 0.0)

    zone_rows.sort(key=sort_key)
    for rank, z in enumerate(zone_rows, start=1):
        z["priority"] = rank

    zone_names = {z.id: z.name for z in zones}
    timeline = []
    for e in db.scalars(select(Event).where(Event.incident_id == incident.id).order_by(Event.id.desc()).limit(80)):
        timeline.append({"t": iso(e.created_at), "kind": e.kind, "message": e.message,
                         "zone": zone_names.get(e.zone_id) if e.zone_id else None})
    for d in decisions:
        timeline.append({"t": iso(d.created_at), "kind": "decision", "action": d.action,
                         "zone": zone_names.get(d.zone_id), "people_count": d.people_count,
                         "note": d.note, "username": d.username})
    timeline.sort(key=lambda item: item["t"] or "", reverse=True)

    rescued_people = sum((d.people_count or 0) for d in decisions if d.action == "rescued")
    counts = {k: 0 for k in STATE_RANK}
    for z in zone_rows:
        counts[z["state"]] += 1

    tx_nodes = [{"id": n.id, "name": n.name, "role": "tx", "zone_id": n.zone_id, "x": n.x, "y": n.y,
                 "status": "tx"} for n in nodes if n.role == "tx"]
    return {
        "incident": incident_out(incident),
        "building": {
            "id": building.id,
            "name": building.name,
            "floor_label": building.floor_label,
            "has_plan": building.plan_mime is not None,
            "plan_width": building.plan_width,
            "plan_height": building.plan_height,
            "is_demo": building.is_demo,
        },
        "mode": registry.mode,
        "thresholds": dict(zip(("detected", "possible"), detection_thresholds(incident.source, registry.model is not None))),
        "model_version": registry.model.version if registry.model else None,
        "server_time": iso(utcnow()),
        "zones": zone_rows,
        "nodes": list(infos.values()) + tx_nodes,
        "timeline": timeline[:100],
        "summary": {
            "states": counts,
            "handled": sum(1 for z in zone_rows if z["decision"] and z["decision"]["action"] in HANDLED_ACTIONS),
            "rescued_people": rescued_people,
            "replay": incident.source == "replay",
            "replay_running": runner.is_running(incident.id),
            "frozen": freeze,
        },
    }
