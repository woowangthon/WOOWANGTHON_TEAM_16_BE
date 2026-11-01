"""학습 명령, 모델 적재, 녹화 재생 시연을 합성 신호로 끝까지 돌려 본다(테스트 전용)."""

import json
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.ml.model import PresenceModel
from app.ml.train import train
from app.services.registry import registry
from synthetic import SyntheticRoom, write_session

CONDITIONS = [("empty", 50), ("lying", 24), ("moving", 24), ("fan", 24), ("behind_wall", 24)]


def _make_dataset(folder: Path) -> None:
    for day_idx, day in enumerate(["2026-11-01", "2026-11-02"]):
        for node, room_seed in [("RX-301", 31), ("RX-302", 32)]:
            room = SyntheticRoom(seed=room_seed)
            t0 = 1_700_100_000.0 + day_idx * 86_400
            for k, (label, secs) in enumerate(CONDITIONS):
                frames = room.frames(secs, label, seed=day_idx * 100 + k + room_seed, t0=t0 + k * 120)
                write_session(folder / day, f"{day}_{node}_{label}", frames,
                              {"node": node, "label": label, "day": day, "zone": node[-3:] + "호"})


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    data = tmp_path_factory.mktemp("sessions")
    _make_dataset(data)
    card = train(data, settings.model_path, "lr", "합성 신호(테스트)")
    yield card
    registry.model = None
    for p in (settings.model_path, settings.model_path.with_suffix(".card.json")):
        p.unlink(missing_ok=True)


def test_train_writes_model_and_card(trained):
    card = trained
    assert card["validation"]["method"] == "leave-one-day-out"
    assert len(card["validation"]["folds"]) == 2
    fold = card["validation"]["folds"][0]
    assert {"model", "stage1_only"} <= fold.keys()
    assert "no_recalibration" in card["validation"]["folds"][1]
    assert card["data"]["windows_by_label"]["fan"] > 0
    assert settings.model_path.exists()
    assert json.loads(settings.model_path.with_suffix(".card.json").read_text(encoding="utf-8"))["status"] == "trained"
    model = PresenceModel.load(settings.model_path)
    p = model.predict(np.zeros(8))
    assert 0.0 <= p <= 1.0


def test_model_separates_fan_from_person_on_synthetic(trained):
    # 1단계(변화 감지)는 선풍기도 '변화'로 보지만, 2단계 모델은 사람과 구분해야 한다.
    fold = trained["validation"]["folds"][0]
    by_label = fold["model"]["by_label"]
    assert by_label["fan"]["rate_at_0.70"] < 0.2
    assert by_label["lying"]["rate_at_0.70"] > 0.8
    assert fold["stage1_only"]["by_label"]["fan"]["rate_at_0.70"] > 0.8


def test_service_uses_model_when_present_and_replay_runs(trained):
    registry.load()
    assert registry.mode == "person"

    scen = settings.sample_data_dir / "synthetic-one-room"
    room = SyntheticRoom(seed=41)
    write_session(scen, "rx_cal", room.frames(40, "empty", seed=1), {"node": "RX-A", "label": "empty", "day": "d"})
    write_session(scen, "rx_stream", room.frames(14, "lying", seed=2, t0=1_700_200_000.0),
                  {"node": "RX-A", "label": "lying", "day": "d"})
    (scen / "scenario.json").write_text(json.dumps({
        "id": "synthetic-one-room",
        "title": "합성 신호 테스트",
        "synthetic": True,
        "zones": [{"name": "A실", "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]}],
        "nodes": [
            {"name": "TX", "role": "tx", "x": 0.1, "y": 0.1},
            {"name": "RX-A", "role": "rx", "x": 0.5, "y": 0.5, "zone": "A실",
             "calibration": "rx_cal.csv", "stream": "rx_stream.csv"},
        ],
        "timeline": [{"at": 0, "text": "A실에 1명 누워 있음"}],
    }, ensure_ascii=False), encoding="utf-8")

    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={"username": "tester", "password": "pw-for-tests"}).json()["token"]
        h = {"Authorization": f"Bearer {token}"}
        listed = client.get("/api/demo/scenarios", headers=h).json()
        assert listed[0]["synthetic"] is True and listed[0]["allowed"] is True
        started = client.post("/api/demo/scenarios/synthetic-one-room/start", json={"speed": 4}, headers=h)
        assert started.status_code == 200, started.text
        incident_id = started.json()["incident_id"]
        assert client.post("/api/demo/scenarios/synthetic-one-room/start", json={"speed": 4}, headers=h).status_code == 409

        deadline = time.time() + 30
        while client.get("/api/demo/status", headers=h).json()["running"] and time.time() < deadline:
            time.sleep(0.3)
        state = client.get(f"/api/incidents/{incident_id}/state", headers=h).json()
        assert state["mode"] == "person"
        assert state["summary"]["replay"] is True and state["summary"]["frozen"] is True
        zone = state["zones"][0]
        assert zone["state"] == "detected", zone
        assert any(e["kind"] == "replay_end" for e in state["timeline"])
        card = client.get("/api/model/card").json()
        assert card["status"] == "trained" and card["mode"] == "person"
