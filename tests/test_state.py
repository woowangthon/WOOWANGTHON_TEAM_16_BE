"""종료된 상황의 판단은 이후 노드 재시작으로 바뀌지 않는다."""

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Baseline, Building, Event, Incident, Inference, Node, utcnow
from app.services.state import _node_infos, _zone_state


@pytest.mark.parametrize("restart_after_close", [True, False])
def test_closed_incident_uses_restart_state_at_close(restart_after_close):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        now = utcnow()
        building = Building(name="종료 기록 확인")
        db.add(building)
        db.flush()
        node = Node(building_id=building.id, name="RX", role="rx", x=0.5, y=0.5, last_seen_at=now)
        db.add(node)
        db.flush()
        incident = Incident(building_id=building.id, source="manual", status="closed",
                            started_at=now - timedelta(minutes=2), ended_at=now - timedelta(minutes=1))
        db.add(incident)
        db.flush()
        db.add(Baseline(node_id=node.id, n_windows=10, n_frames=1000, n_subcarriers=52,
                        stats={}, active=True, created_at=now - timedelta(minutes=3)))
        db.add(Inference(incident_id=incident.id, node_id=node.id, t=now - timedelta(seconds=100),
                         change_score=10.0, p_change=0.9, quality=1.0, z={}, source="live"))
        db.commit()

        before = _node_infos(db, incident, [node], freeze=True)[node.id]
        assert _zone_state([before])[0] == "detected"

        restart_at = now - timedelta(seconds=30 if restart_after_close else 90)
        db.add(Event(building_id=building.id, node_id=node.id,
                     incident_id=None if restart_after_close else incident.id,
                     kind="node_restart", message="노드 재시작", created_at=restart_at))
        db.commit()

        after = _node_infos(db, incident, [node], freeze=True)[node.id]
        if restart_after_close:
            assert after["status"] == "ok" and after["reason"] is None
            assert _zone_state([after])[0] == "detected"
        else:
            assert after["status"] == "stale" and after["reason"] == "restarted"
            assert _zone_state([after])[0] == "no_signal"
    engine.dispose()
