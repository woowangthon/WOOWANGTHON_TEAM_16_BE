"""판정 정책이 모델 카드·구역 상태·첫 감지에 동일하게 적용되는지 확인한다."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.main import app
from app.ml.csi import Frame
from app.models import Baseline, Building, Event, Incident, Inference, Node, Zone
from app.routers import ingest as ingest_router
from app.services import state, stream
from app.services.registry import registry

SOURCES = ("manual", "alarm", "measurement", "replay")


class FixedScoreBaseline:
    """특징 추출만 단순화하여 프레임 진폭을 알려진 확률로 전달한다."""

    n_subcarriers = 8
    fps = 50

    def features(self, amp, rssi, context_amp, context_t):
        if not len(amp):
            return None
        return np.full(8, float(np.median(amp[:, 0])))

    def zscore(self, features):
        return features

    def change_score(self, z):
        return float(z[0])

    def p_change(self, score):
        return score


class FixedScoreModel:
    version = "policy-test"
    card = {"status": "trained", "model_version": version}

    def predict(self, z):
        return float(z[0])


@pytest.fixture
def policy_scene(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(stream, "utcnow", lambda: now)
    monkeypatch.setattr(state, "utcnow", lambda: now)
    monkeypatch.setattr(registry, "model", None)
    hub = stream.StreamHub()
    monkeypatch.setattr(hub, "baseline_for", lambda db, node_id: FixedScoreBaseline())
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        building = Building(name="판정 정책")
        db.add(building)
        db.flush()
        zone = Zone(building_id=building.id, name="A", polygon=[[0, 0], [1, 0], [1, 1]])
        db.add(zone)
        db.flush()
        node = Node(building_id=building.id, zone_id=zone.id, name="RX", role="rx", x=0.5, y=0.5)
        db.add(node)
        db.flush()
        db.add(Baseline(node_id=node.id, created_at=now - timedelta(minutes=2), n_windows=10,
                        n_frames=1000, n_subcarriers=8, stats={}, active=True))
        incident = Incident(building_id=building.id, source="manual", status="active",
                            started_at=now - timedelta(minutes=1))
        db.add(incident)
        db.commit()
        yield db, node, incident, hub
    engine.dispose()


def send_window(scene, index, p, fps=50):
    db, node, incident, hub = scene
    start = 100 + 2 * index
    frames = [Frame(start + i / fps, -45, np.full(8, p)) for i in range(2 * fps + 1)]
    source = "replay" if incident.source == "replay" else "live"
    result = hub.ingest(db, node, frames, source=source)
    assert result["windows"] == 1
    info = state._node_infos(db, incident, [node])[node.id]
    zone = state._zone_state([info], incident.source)
    events = db.scalars(select(Event).where(Event.incident_id == incident.id,
                                           Event.kind == "first_detection")).all()
    return info, zone, events


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("person_mode", [False, True], ids=["change", "person"])
def test_card_state_and_first_detection_share_effective_policy(policy_scene, monkeypatch, source, person_mode):
    db, node, incident, hub = policy_scene
    incident.source = source
    db.commit()
    monkeypatch.setattr(registry, "model", FixedScoreModel() if person_mode else None)
    expected = {"detected": 0.5, "possible": 0.2} if source in {"measurement", "replay"} and not person_mode else {"detected": 0.7, "possible": 0.35}

    # 시작 훅이 모델을 읽은 뒤 이 시험의 모델 유무를 적용한다.
    with TestClient(app) as client:
        monkeypatch.setattr(registry, "model", FixedScoreModel() if person_mode else None)
        card_response = client.get("/api/model/card", params={"source": source})
    assert card_response.status_code == 200
    card = card_response.json()
    assert card["mode"] == ("person" if person_mode else "change")
    assert card["runtime"]["source"] == source
    assert card["runtime"]["thresholds"] == expected
    assert card["runtime"]["thresholds_by_source"][source] == expected

    info, zone, events = send_window(policy_scene, 0, 0.2)
    assert zone[0] != "detected" and not events
    info, zone, events = send_window(policy_scene, 1, 0.8)
    assert info["p"] == 0.5
    should_detect = expected["detected"] == 0.5
    assert (zone[0] == "detected") is should_detect
    assert bool(events) is should_detect

    info, zone, events = send_window(policy_scene, 2, 0.9)
    assert info["p"] == 0.7
    assert zone[0] == "detected"
    assert len(events) == 1
    response = state.incident_state(db, incident)
    assert response["thresholds"] == expected
    assert response["mode"] == card["mode"]
    assert response["zones"][0]["state"] == "detected"


@pytest.mark.parametrize("source", SOURCES)
def test_low_quality_spike_cannot_start_first_detection(policy_scene, source):
    db, node, incident, hub = policy_scene
    incident.source = source
    db.commit()
    send_window(policy_scene, 0, 0.2)
    info, zone, events = send_window(policy_scene, 1, 1.0, fps=10)
    assert info["quality"] < 0.7
    assert info["p"] == 0.2
    assert info["reason"] == "low_rate"
    assert zone[0] == "no_signal" and not events
    info, zone, events = send_window(policy_scene, 2, 0.2)
    assert info["p"] == 0.2
    assert zone[0] != "detected" and not events


def test_model_card_default_and_invalid_source_preserve_api_contract():
    with TestClient(app) as client:
        default = client.get("/api/model/card").json()
        explicit = client.get("/api/model/card", params={"source": "manual"}).json()
        invalid = client.get("/api/model/card", params={"source": "measurement_typo"})
    assert default["runtime"]["source"] == "manual"
    assert default["runtime"]["thresholds"] == {"detected": 0.7, "possible": 0.35}
    assert default["runtime"] == explicit["runtime"]
    assert set(default["runtime"]["thresholds_by_source"]) == set(SOURCES)
    assert invalid.status_code == 422


@pytest.mark.parametrize("source", ["manual", "alarm"])
def test_first_detection_waits_for_smoothed_threshold(policy_scene, source):
    db, node, incident, hub = policy_scene
    incident.source = source
    db.commit()
    send_window(policy_scene, 0, 0.2)
    info, zone, events = send_window(policy_scene, 1, 0.8)
    assert info["p"] == 0.5
    assert zone[0] == "possible"
    assert not events  # 현재 창만 0.8이어도 화면의 평활 확률은 0.5다.


@pytest.mark.parametrize("previous_source", ["measurement", "manual"])
def test_alarm_promotion_resets_only_measurement_scores(policy_scene, monkeypatch, previous_source):
    db, node, incident, hub = policy_scene
    monkeypatch.setattr(ingest_router, "hub", hub)
    incident.source = previous_source
    db.commit()
    info, zone, old_events = send_window(policy_scene, 0, 0.8)
    assert zone[0] == "detected" and len(old_events) == 1
    old_row = db.scalar(select(Inference).where(Inference.incident_id == incident.id))
    old_event_id = old_events[0].id

    same, created = ingest_router.open_incident(db, db.get(Building, node.building_id), "alarm", "receiver")
    assert same.id == incident.id and not created
    result = state.incident_state(db, incident)
    reset = previous_source == "measurement"
    if reset:
        assert result["zones"][0]["p"] is None
        assert result["zones"][0]["detected_since"] is None
        phase = db.scalar(select(Event).where(Event.incident_id == incident.id,
                                             Event.kind == "detection_reset"))
        now = stream.as_aware(phase.created_at) + timedelta(seconds=2)
    else:
        assert result["zones"][0]["p"] == 0.8
        assert result["zones"][0]["detected_since"] is not None
        now = datetime.now(timezone.utc) + timedelta(seconds=2)
    monkeypatch.setattr(stream, "utcnow", lambda: now)
    monkeypatch.setattr(state, "utcnow", lambda: now)
    assert db.get(Inference, old_row.id) is not None
    assert db.get(Event, old_event_id) is not None

    info, zone, events = send_window(policy_scene, 1, 0.2)
    assert info["p"] == (0.2 if reset else 0.5)
    assert len(events) == 1
    info, zone, events = send_window(policy_scene, 2, 1.0)
    assert info["p"] == (0.6 if reset else 0.75)
    assert len(events) == 1
    info, zone, events = send_window(policy_scene, 3, 1.0)
    assert info["p"] == (0.8 if reset else 0.875)
    assert len(events) == (2 if reset else 1)
    assert state.incident_state(db, incident)["zones"][0]["detected_since"] is not None


def test_first_detection_and_state_share_rounded_quality_boundary(policy_scene, monkeypatch):
    db, node, incident, hub = policy_scene
    incident.source = "measurement"
    db.commit()
    base = FixedScoreBaseline()
    base.fps = 50.01
    monkeypatch.setattr(hub, "baseline_for", lambda db, node_id: base)
    # 70 / (50.01 * 2) = 0.69986...; 저장된 품질은 0.700이다.
    info, zone, events = send_window(policy_scene, 0, 0.8, fps=35)
    assert info["quality"] == 0.7
    assert zone[0] == "detected"
    assert len(events) == 1


def test_post_promotion_bulk_batch_ignores_pre_phase_high_scores(policy_scene):
    db, node, incident, hub = policy_scene
    incident.source = "alarm"
    now = stream.utcnow()
    db.add(Event(building_id=node.building_id, incident_id=incident.id, kind="detection_reset",
                 message="measurement to alarm", created_at=now - timedelta(seconds=2)))
    db.commit()
    frames = [Frame(100 + i / 50, -45, np.full(8, 0.9 if i < 800 else 0.1))
              for i in range(1001)]
    result = hub.ingest(db, node, frames)
    assert result["windows"] == 10
    rows = db.scalars(select(Inference).where(Inference.incident_id == incident.id)).all()
    assert len(rows) == 10  # 이전 구간의 추론도 감사 기록으로 보존한다.
    current = stream.recent_probabilities(db, incident.id, node.id)
    assert len(current) == 2
    assert all(row.p_change == 0.1 for row in current)
    info = state._node_infos(db, incident, [node])[node.id]
    assert info["p"] == 0.1
    assert state._zone_state([info], incident.source)[0] != "detected"
    assert not db.scalars(select(Event).where(Event.incident_id == incident.id,
                                              Event.kind == "first_detection")).all()
    assert state.incident_state(db, incident)["zones"][0]["detected_since"] is None
