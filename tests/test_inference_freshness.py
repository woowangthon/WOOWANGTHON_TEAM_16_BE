"""통신이 이어져도 마지막 유효 판단이 오래되면 판단 불가로 표시한다."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.ml.baseline import Baseline
from app.ml.csi import Frame
from app.models import Building, Event, Incident, Inference, Node, Zone
from app.routers import ingest as ingest_router
from app.schemas import IngestIn
from app.services import state, stream


@pytest.fixture
def scene(monkeypatch):
    clock = [datetime(2026, 11, 8, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(state, "utcnow", lambda: clock[0])
    monkeypatch.setattr(stream, "utcnow", lambda: clock[0])
    hub = stream.StreamHub()
    monkeypatch.setattr(ingest_router, "hub", hub)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        building = Building(name="유효 판단 신선도")
        db.add(building)
        db.flush()
        zone = Zone(building_id=building.id, name="A", polygon=[[0, 0], [1, 0], [1, 1]])
        db.add(zone)
        db.flush()
        node = Node(building_id=building.id, zone_id=zone.id, name="RX", role="rx",
                    x=0.5, y=0.5, last_seen_at=clock[0])
        db.add(node)
        db.flush()
        incident = Incident(building_id=building.id, source="manual", status="active",
                            started_at=clock[0] - timedelta(minutes=1))
        db.add(incident)
        db.flush()
        base = Baseline(mu=np.ones(8), mask=np.ones(8, dtype=bool), rssi_ref=-45,
                        fps=50, med=np.zeros(8), mad=np.ones(8), tau=2,
                        n_windows=10, n_frames=1000)
        baseline = hub.store_baseline(db, node, base)
        baseline.created_at = clock[0] - timedelta(minutes=2)
        for seconds in (5, 3, 1):
            db.add(Inference(incident_id=incident.id, node_id=node.id, zone_id=zone.id,
                             t=clock[0] - timedelta(seconds=seconds), change_score=10,
                             p_change=0.9, quality=1, z={}, source="live"))
        db.commit()
        # 이미 처리한 마지막 프레임. 같은 시각이 다시 오면 새 창은 생기지 않는다.
        buf = stream._NodeBuffer()
        buf.reset(incident.id)
        buf.t.append(100.0)
        buf.rssi.append(-45.0)
        buf.amp.append(np.ones(8))
        buf.next_end = 102.0
        hub._buffers[node.id] = buf
        yield db, node, incident, hub, clock
    engine.dispose()


@pytest.mark.parametrize("traffic", ["invalid", "duplicate", "mismatch"])
def test_rejected_only_traffic_cannot_keep_old_inference_current(scene, traffic):
    db, node, incident, hub, clock = scene
    before = state._node_infos(db, incident, [node])[node.id]
    assert before["status"] == "ok"
    assert state._zone_state([before])[0] == "detected"

    clock[0] += timedelta(seconds=10)
    data, t = [0, 1] * 8, 100.0
    if traffic == "invalid":
        data, t = [1, 2, 3], 101.0  # 파싱 단계에서 버려져 hub에는 빈 목록이 전달된다.
    elif traffic == "mismatch":
        data, t = [0, 1] * 4, 101.0
    body = IngestIn(frames=[{"t": t, "rssi": -45, "data": data}])
    ingest_router.ingest_csi(body, node=node, db=db)
    rows = db.scalars(select(Inference).where(Inference.incident_id == incident.id)).all()
    assert len(rows) == 3  # 새 통신은 있었지만 새 판단은 없다.
    assert stream.as_aware(node.last_seen_at) == clock[0]
    stale = state._node_infos(db, incident, [node])[node.id]
    assert stale["status"] == "stale"
    assert stale["reason"] == "stale_inference"
    assert state._zone_state([stale])[0] == "no_signal"

    # 다시 정상 프레임으로 새 판단이 만들어지면 복구된다.
    frames = [Frame(100.02 + i / 50, -45, np.ones(8)) for i in range(101)]
    result = hub.ingest(db, node, frames)
    assert result["windows"] == 1
    recovered = state._node_infos(db, incident, [node])[node.id]
    assert recovered["status"] == "ok"
    assert recovered["reason"] is None


def test_low_rate_and_restart_reasons_take_priority(scene):
    db, node, incident, hub, clock = scene
    clock[0] += timedelta(seconds=10)
    node.last_seen_at = clock[0]
    db.add(Inference(incident_id=incident.id, node_id=node.id, zone_id=node.zone_id,
                     t=clock[0], change_score=10, p_change=1, quality=0.3, z={}, source="live"))
    db.commit()
    low_rate = state._node_infos(db, incident, [node])[node.id]
    assert low_rate["status"] == "stale" and low_rate["reason"] == "low_rate"

    db.add(Event(building_id=node.building_id, incident_id=incident.id, node_id=node.id,
                 kind="node_restart", message="재시작", created_at=clock[0]))
    db.commit()
    restarted = state._node_infos(db, incident, [node])[node.id]
    assert restarted["status"] == "stale" and restarted["reason"] == "restarted"


@pytest.mark.parametrize("source,status", [("manual", "closed"), ("replay", "active")])
def test_frozen_incident_preserves_last_valid_inference(scene, source, status, monkeypatch):
    db, node, incident, hub, clock = scene
    incident.source, incident.status = source, status
    if status == "closed":
        incident.ended_at = clock[0]
    db.commit()
    clock[0] += timedelta(hours=1)
    from app.services.replay import runner
    monkeypatch.setattr(runner, "is_running", lambda incident_id=None: False)
    result = state.incident_state(db, incident)
    assert result["summary"]["frozen"] is True
    assert result["nodes"][0]["status"] == "ok"
    assert result["zones"][0]["state"] == "detected"


@pytest.mark.parametrize("age,expected", [(6.0, "ok"), (6.001, "stale")])
def test_inference_freshness_uses_six_second_boundary(scene, age, expected):
    db, node, incident, hub, clock = scene
    latest = db.scalars(select(Inference).order_by(Inference.id.desc())).first()
    latest.t = clock[0] - timedelta(seconds=age)
    db.commit()
    info = state._node_infos(db, incident, [node])[node.id]
    assert info["status"] == expected
