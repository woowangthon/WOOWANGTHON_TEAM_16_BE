"""Calibration fitting must release the stream lock and discard obsolete input.

Each worker uses its own Session against a file-backed SQLite database. Events
hold fitting at a known point so concurrency assertions do not depend on sleeps.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db import Base, _make_engine
from app.ml.baseline import Baseline
from app.ml.csi import Frame
from app.models import Baseline as BaselineRow
from app.models import Building, CalibrationRun, Event, Incident, Node, utcnow
from app.routers import ingest as ingest_router
from app.services import stream


def _base() -> Baseline:
    return Baseline(np.ones(8), np.ones(8, dtype=bool), -45.0, 10.0,
                    np.zeros(8), np.ones(8), 2.0, 19, 400)


def _frames(t0=1000.0):
    return [Frame(t0 + i / 10, -45.0, np.ones(8)) for i in range(400)]


@dataclass
class Worker:
    action: Callable
    done: threading.Event = field(default_factory=threading.Event)
    values: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def __post_init__(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            self.values.append(self.action())
        except BaseException as exc:
            self.errors.append(exc)
        finally:
            self.done.set()

    def join(self):
        self.thread.join(5)
        assert not self.thread.is_alive(), "calibration test worker did not stop"
        if self.errors:
            raise self.errors[0]


class PausedFit:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(10), "test did not release paused fitting"
        return _base()


def _cleanup(paused, *workers):
    paused.release.set()
    errors = []
    for worker in workers:
        if worker is not None:
            try:
                worker.join()
            except BaseException as exc:
                errors.append(exc)
    if errors:
        raise errors[0]


@pytest.fixture
def lab(tmp_path, monkeypatch):
    engine = _make_engine(f"sqlite:///{tmp_path / 'calibration.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    hub = stream.StreamHub()
    monkeypatch.setattr(ingest_router, "hub", hub)
    # An asynchronous implementation may use the module's session factory.
    monkeypatch.setattr(stream, "SessionLocal", sessions, raising=False)
    yield hub, sessions
    engine.dispose()


def _collect(lab, nodes=1):
    hub, sessions = lab
    with sessions() as db:
        building = Building(name="fitting room")
        other = Building(name="other room")
        db.add_all([building, other])
        db.flush()
        receivers = [Node(building_id=building.id, name=f"RX-{i}", role="rx", x=.5, y=.5)
                     for i in range(nodes)]
        other_node = Node(building_id=other.id, name="other RX", role="rx", x=.5, y=.5)
        db.add_all([*receivers, other_node])
        db.commit()
        run = hub.start_calibration(db, building.id, 60)
        for node in receivers:
            hub.ingest(db, node, _frames())
        run.ends_at = utcnow() - timedelta(seconds=1)
        db.commit()
        return building.id, run.id, [n.id for n in receivers], other_node.id


def _finish(lab, run_id):
    hub, sessions = lab
    with sessions() as db:
        return hub.finish_calibration(db, db.get(CalibrationRun, run_id)).status


def _pause(monkeypatch):
    paused = PausedFit()
    monkeypatch.setattr(stream, "fit_baseline", paused)
    return paused


def test_ingest_other_building_does_not_wait_for_calibration_fit(lab, monkeypatch):
    hub, sessions = lab
    _, run_id, _, other_node = _collect(lab)
    paused = _pause(monkeypatch)
    owner = Worker(lambda: _finish(lab, run_id))
    actor = None
    responsive = False
    try:
        assert paused.entered.wait(3), "fitting did not start"

        def ingest():
            with sessions() as db:
                return hub.ingest(db, db.get(Node, other_node), _frames())

        actor = Worker(ingest)
        responsive = actor.done.wait(1)
    finally:
        _cleanup(paused, owner, actor)
    assert responsive, "another building's ingest waited for the calibration fit's global lock"
    assert actor.values[0]["mode"] == "idle"


def test_expired_calibration_lookup_does_not_wait_for_existing_fit(lab, monkeypatch):
    hub, sessions = lab
    building_id, run_id, _, _ = _collect(lab)
    paused = _pause(monkeypatch)
    owner = Worker(lambda: _finish(lab, run_id))
    actor = None
    responsive = False
    try:
        assert paused.entered.wait(3), "fitting did not start"

        def lookup():
            with sessions() as db:
                run = hub.active_calibration(db, building_id)
                return None if run is None else run.id

        actor = Worker(lookup)
        responsive = actor.done.wait(1)
    finally:
        _cleanup(paused, owner, actor)
    assert responsive, "a lookup tried to finish the same calibration and waited behind fitting"
    assert paused.calls == 1


def test_duplicate_finish_fits_once_and_keeps_success(lab, monkeypatch):
    _, sessions = lab
    _, run_id, _, _ = _collect(lab)
    paused = _pause(monkeypatch)
    owner = Worker(lambda: _finish(lab, run_id))
    duplicate = None
    try:
        assert paused.entered.wait(3)
        duplicate = Worker(lambda: _finish(lab, run_id))
    finally:
        _cleanup(paused, owner, duplicate)
    assert paused.calls == 1
    with sessions() as db:
        run = db.get(CalibrationRun, run_id)
        assert run.status == "done"
        assert all(item["ok"] for item in run.result.values())
        assert len(db.scalars(select(BaselineRow)).all()) == 1


def test_restart_during_fit_discards_that_nodes_snapshot(lab, monkeypatch):
    hub, sessions = lab
    _, run_id, nodes, _ = _collect(lab)
    paused = _pause(monkeypatch)
    owner = Worker(lambda: _finish(lab, run_id))
    actor = None
    responsive = False
    try:
        assert paused.entered.wait(3)

        def restart():
            with sessions() as db:
                return hub.ingest(db, db.get(Node, nodes[0]), _frames(2000), restarted=True)

        actor = Worker(restart)
        responsive = actor.done.wait(1)
    finally:
        _cleanup(paused, owner, actor)
    assert responsive, "a restart could not invalidate the snapshot while fitting"
    with sessions() as db:
        assert db.get(CalibrationRun, run_id).result[str(nodes[0])]["ok"] is False
        assert db.scalar(select(BaselineRow.id).where(BaselineRow.node_id == nodes[0],
                                                    BaselineRow.active.is_(True))) is None
        assert db.scalar(select(Event.id).where(Event.node_id == nodes[0], Event.kind == "node_restart"))


def test_new_calibration_during_fit_preserves_new_run_and_buffer(lab, monkeypatch):
    hub, sessions = lab
    building_id, old_id, nodes, _ = _collect(lab)
    paused = _pause(monkeypatch)
    owner = Worker(lambda: _finish(lab, old_id))
    actor = None
    responsive = False
    try:
        assert paused.entered.wait(3)

        def replace():
            with sessions() as db:
                new = hub.start_calibration(db, building_id, 60)
                hub.ingest(db, db.get(Node, nodes[0]), _frames(3000))
                return new.id

        actor = Worker(replace)
        responsive = actor.done.wait(1)
    finally:
        _cleanup(paused, owner, actor)
    assert responsive, "a new calibration waited for an obsolete fit instead of invalidating it"
    with sessions() as db:
        assert db.get(CalibrationRun, old_id).status == "failed"
        assert db.get(CalibrationRun, actor.values[0]).status == "running"
        assert db.scalar(select(BaselineRow.id)) is None
    assert hub.calibration_progress(nodes)[nodes[0]]["frames"] == 400


def test_alarm_during_fit_keeps_calibration_cancelled(lab, monkeypatch):
    _, sessions = lab
    building_id, run_id, _, _ = _collect(lab)
    paused = _pause(monkeypatch)
    owner = Worker(lambda: _finish(lab, run_id))
    actor = None
    responsive = False
    try:
        assert paused.entered.wait(3)

        def alarm():
            with sessions() as db:
                incident, created = ingest_router.open_incident(db, db.get(Building, building_id), "alarm", "")
                return incident.id, created

        actor = Worker(alarm)
        responsive = actor.done.wait(1)
    finally:
        _cleanup(paused, owner, actor)
    assert responsive, "the alarm waited for calibration fitting"
    with sessions() as db:
        assert db.get(CalibrationRun, run_id).status == "failed", "obsolete fitting overwrote alarm cancellation"
        assert db.scalar(select(BaselineRow.id)) is None, "a baseline from before the alarm was applied"
        assert db.get(Incident, actor.values[0][0]).status == "active"


def test_one_node_fit_exception_does_not_stop_other_nodes_or_next_run(lab, monkeypatch):
    hub, sessions = lab
    building_id, run_id, nodes, _ = _collect(lab, nodes=2)
    calls = 0

    def flaky(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("failed numerical fit")
        return _base()

    monkeypatch.setattr(stream, "fit_baseline", flaky)
    assert _finish(lab, run_id) == "done"
    with sessions() as db:
        run = db.get(CalibrationRun, run_id)
        assert run.result[str(nodes[0])]["ok"] is False
        assert run.result[str(nodes[1])]["ok"] is True
        assert db.scalar(select(BaselineRow.id).where(BaselineRow.node_id == nodes[0])) is None
        assert db.scalar(select(BaselineRow.id).where(BaselineRow.node_id == nodes[1])) is not None
        new = hub.start_calibration(db, building_id, 60)
        for node_id in nodes:
            hub.ingest(db, db.get(Node, node_id), _frames(4000))
        new_id = new.id
    monkeypatch.setattr(stream, "fit_baseline", lambda *args, **kwargs: _base())
    assert _finish(lab, new_id) == "done"


def test_first_ingest_after_expiry_starts_fit_without_waiting(lab, monkeypatch):
    hub, sessions = lab
    _, run_id, nodes, _ = _collect(lab)
    paused = _pause(monkeypatch)
    ingress = None
    responsive = False
    try:
        def ingest():
            with sessions() as db:
                return hub.ingest(db, db.get(Node, nodes[0]), _frames(5000))

        ingress = Worker(ingest)
        assert paused.entered.wait(3), "expired ingest did not start fitting"
        responsive = ingress.done.wait(1)
    finally:
        # Public synchronous finish waits for the asynchronous owner, so no
        # background computation can outlive this test's database/monkeypatch.
        finisher = Worker(lambda: _finish(lab, run_id))
        _cleanup(paused, ingress, finisher)
    assert responsive, "the first ingest after expiry waited for automatic fitting"
    assert ingress.values[0]["mode"] == "calibrating"
    assert ingress.values[0]["accepted"] == 0
    assert paused.calls == 1
    with sessions() as db:
        assert db.get(CalibrationRun, run_id).status == "done"
        assert len(db.scalars(select(BaselineRow)).all()) == 1
