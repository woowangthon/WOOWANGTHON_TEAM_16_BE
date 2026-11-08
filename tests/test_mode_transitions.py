"""Real alarms must have explicit priority over measurements and test alarms."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db import Base, _make_engine
from app.models import Building, Event, Incident, Zone, utcnow
from app.routers import ingest
from app.schemas import AlarmIn
from app.services import state
from app.services.stream import StreamHub


@pytest.fixture
def lab(tmp_path, monkeypatch):
    engine = _make_engine(f"sqlite:///{tmp_path / 'modes.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(ingest, "hub", StreamHub())
    yield sessions
    engine.dispose()


def _building(db, source=None, demo=False):
    building = Building(name="mode transition", is_demo=demo)
    db.add(building)
    db.flush()
    incident = None
    if source:
        incident = Incident(building_id=building.id, source=source)
        db.add(incident)
    db.commit()
    return building, incident


def _events(db, incident_id, kind):
    return db.scalars(select(Event).where(Event.incident_id == incident_id, Event.kind == kind)
                      .order_by(Event.id)).all()


@pytest.mark.parametrize("source", ["measurement", "manual"])
def test_real_fire_promotes_existing_incident_without_changing_id(lab, source):
    with lab() as db:
        building, previous = _building(db, source)
        result = ingest.fire_alarm(AlarmIn(event="fire", detail="receiver 7"), building, db)
        db.refresh(previous)
        assert result == {"incident_id": previous.id, "created": False}
        assert previous.source == "alarm", "a real alarm was absorbed by a lower-priority mode"
        assert previous.status == "active"
        assert len(_events(db, previous.id, "mode_changed")) == 1
        received = _events(db, previous.id, "alarm_received")
        assert len(received) == 1 and "receiver 7" in received[0].message


def test_repeated_fire_is_recorded_without_duplicate_incidents(lab):
    with lab() as db:
        building, previous = _building(db, "alarm")
        for detail in ("receiver one", "receiver two"):
            result = ingest.fire_alarm(AlarmIn(event="fire", detail=detail), building, db)
            assert result == {"incident_id": previous.id, "created": False}
        assert len(_events(db, previous.id, "alarm_received")) == 2
        assert not _events(db, previous.id, "mode_changed")
        assert len(db.scalars(select(Incident).where(Incident.status == "active")).all()) == 1


@pytest.mark.parametrize("requested", ["measurement", "manual"])
def test_measurement_and_manual_requests_cannot_downgrade_alarm(lab, requested):
    with lab() as db:
        building, previous = _building(db, "alarm")
        incident, created = ingest.open_incident(db, building, requested, "ordinary request")
        db.refresh(previous)
        assert incident.id == previous.id and created is False
        assert previous.source == "alarm"


def test_test_alarm_does_not_promote_measurement_to_real_fire(lab):
    with lab() as db:
        building, previous = _building(db, "measurement")
        result = ingest.fire_alarm(AlarmIn(event="test", detail="exercise"), building, db)
        db.refresh(previous)
        assert result == {"incident_id": previous.id, "created": False}
        assert previous.source == "measurement"
        assert len(_events(db, previous.id, "alarm_test")) == 1
        assert not _events(db, previous.id, "alarm_received")
        assert not _events(db, previous.id, "mode_changed")


def test_test_alarm_without_incident_starts_manual_exercise(lab):
    with lab() as db:
        building, _ = _building(db)
        result = ingest.fire_alarm(AlarmIn(event="test"), building, db)
        incident = db.get(Incident, result["incident_id"])
        assert result["created"] is True
        assert incident.source == "manual", "a test alarm must not create a real-fire incident"
        assert "시험" in incident.note
        assert len(_events(db, incident.id, "alarm_test")) == 1
        assert not _events(db, incident.id, "alarm_received")


def test_real_alarm_on_demo_building_is_explicitly_rejected(lab):
    with lab() as db:
        building, replay = _building(db, "replay", demo=True)
        with pytest.raises(HTTPException) as error:
            ingest.fire_alarm(AlarmIn(event="fire"), building, db)
        assert error.value.status_code == 409
        db.refresh(replay)
        assert replay.source == "replay" and replay.status == "active"
        assert not _events(db, replay.id, "alarm_received")
        assert len(db.scalars(select(Incident)).all()) == 1


def test_real_alarm_on_non_demo_replay_is_also_rejected(lab):
    with lab() as db:
        building, replay = _building(db, "replay")
        with pytest.raises(HTTPException) as error:
            ingest.fire_alarm(AlarmIn(event="fire"), building, db)
        assert error.value.status_code == 409
        db.refresh(replay)
        assert replay.source == "replay" and replay.status == "active"
        assert not _events(db, replay.id, "alarm_received")
        assert len(db.scalars(select(Incident)).all()) == 1


@pytest.mark.parametrize("source, reset", [("measurement", True), ("manual", False)])
def test_promotion_separates_measurement_detection_but_keeps_audit(lab, source, reset):
    with lab() as db:
        building, previous = _building(db, source)
        zone = Zone(building_id=building.id, name="room", polygon=[[0, 0], [1, 0], [1, 1]])
        db.add(zone)
        db.flush()
        first = Event(building_id=building.id, incident_id=previous.id, zone_id=zone.id,
                      kind="first_detection", message="previous detection",
                      created_at=utcnow() - timedelta(minutes=1))
        db.add(first)
        db.commit()
        first_id = first.id
        ingest.fire_alarm(AlarmIn(event="fire"), building, db)
        db.refresh(previous)
        result = state.incident_state(db, previous)
        assert db.get(Event, first_id).kind == "first_detection", "historical audit was removed"
        assert len(_events(db, previous.id, "detection_reset")) == int(reset)
        if reset:
            assert result["zones"][0]["detected_since"] is None, "measurement detection leaked into real alarm"
        else:
            assert result["zones"][0]["detected_since"] is not None


def test_stale_second_request_records_alarm_without_repeating_mode_reset(lab):
    with lab() as db:
        building, previous = _building(db, "measurement")
        building_id, incident_id = building.id, previous.id
    with lab() as first, lab() as second:
        first_building = first.get(Building, building_id)
        second_building = second.get(Building, building_id)
        stale = second.get(Incident, incident_id)
        assert stale.source == "measurement"
        ingest.fire_alarm(AlarmIn(event="fire"), first_building, first)
        ingest.fire_alarm(AlarmIn(event="fire"), second_building, second)
        second.refresh(stale)
        assert stale.source == "alarm"
        assert len(_events(second, incident_id, "alarm_received")) == 2
        assert len(_events(second, incident_id, "mode_changed")) == 1
        assert len(_events(second, incident_id, "detection_reset")) == 1
