"""설치 → 보정 → 평상시 → 경보 → 감지 → 결정 → 종료까지 API로 한 번에 돌려 본다(합성 신호, 테스트 전용)."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Inference
from app.db import SessionLocal
from synthetic import SyntheticRoom

BATCH = 50  # 약 1초 분량씩 보낸다


def _send(client, token, frames, source="live"):
    last = None
    for i in range(0, len(frames), BATCH):
        chunk = frames[i:i + BATCH]
        body = {"frames": [{"t": t, "rssi": r, "data": raw} for t, r, raw in chunk], "source": source}
        last = client.post("/api/ingest/csi", json=body, headers={"X-Node-Token": token})
        assert last.status_code == 200, last.text
    return last.json()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def auth(client):
    r = client.post("/api/auth/login", json={"username": "tester", "password": "pw-for-tests"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_login_and_guards(client):
    assert client.post("/api/auth/login", json={"username": "tester", "password": "wrong"}).status_code == 401
    assert client.get("/api/buildings").status_code == 401
    assert client.get("/api/buildings", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/api/ingest/csi", json={"frames": [{"t": 1, "data": [1, 2]}]}).status_code == 401
    assert client.post("/api/alarm", json={}, headers={"X-Alarm-Token": "nope"}).status_code == 401


def test_full_incident_flow(client, auth):
    room_a, room_b = SyntheticRoom(seed=11), SyntheticRoom(seed=12)

    b = client.post("/api/buildings", json={"name": "테스트관", "floor_label": "3층"}, headers=auth)
    assert b.status_code == 201
    building = b.json()
    alarm_token = building["alarm_token"]
    bid = building["id"]

    za = client.post(f"/api/buildings/{bid}/zones", json={"name": "301호", "polygon": [[0, 0], [0.5, 0], [0.5, 1], [0, 1]]}, headers=auth).json()
    zb = client.post(f"/api/buildings/{bid}/zones", json={"name": "302호", "polygon": [[0.5, 0], [1, 0], [1, 1], [0.5, 1]]}, headers=auth).json()
    bad_zone = client.post(f"/api/buildings/{bid}/zones", json={"name": "x", "polygon": [[0, 0], [2, 0], [0, 1]]}, headers=auth)
    assert bad_zone.status_code == 422

    tx = client.post(f"/api/buildings/{bid}/nodes", json={"name": "TX-1", "role": "tx", "x": 0.5, "y": 0.1}, headers=auth).json()
    assert "token" not in tx
    rx_a = client.post(f"/api/buildings/{bid}/nodes", json={"name": "RX-301", "role": "rx", "x": 0.25, "y": 0.5, "zone_id": za["id"]}, headers=auth).json()
    rx_b = client.post(f"/api/buildings/{bid}/nodes", json={"name": "RX-302", "role": "rx", "x": 0.75, "y": 0.5, "zone_id": zb["id"]}, headers=auth).json()
    tok_a, tok_b = rx_a["token"], rx_b["token"]

    # 평상시: CSI는 버린다
    idle = _send(client, tok_a, room_a.frames(2, "empty", seed=1))
    assert idle["mode"] == "idle" and idle["accepted"] == 0

    # 보정
    r = client.post(f"/api/buildings/{bid}/calibration/start", json={"duration_seconds": 120}, headers=auth)
    assert r.status_code == 200 and r.json()["calibration"]["status"] == "running"
    assert _send(client, tok_a, room_a.frames(40, "empty", seed=2))["mode"] == "calibrating"
    _send(client, tok_b, room_b.frames(40, "empty", seed=3))
    done = client.post(f"/api/buildings/{bid}/calibration/stop", headers=auth).json()
    assert done["calibration"]["status"] == "done"
    assert all(n["calibrated"] for n in done["nodes"] if n["role"] == "rx")

    # 경보 → 상황 열림
    alarm = client.post("/api/alarm", json={"event": "fire"}, headers={"X-Alarm-Token": alarm_token}).json()
    assert alarm["created"] is True
    incident_id = alarm["incident_id"]
    again = client.post("/api/alarm", json={"event": "fire"}, headers={"X-Alarm-Token": alarm_token}).json()
    assert again == {"incident_id": incident_id, "created": False}
    assert client.post(f"/api/buildings/{bid}/calibration/start", json={"duration_seconds": 60}, headers=auth).status_code == 409

    # 301호에는 누워 있는 사람, 302호는 비어 있음
    res = _send(client, tok_a, room_a.frames(24, "lying", seed=4, t0=1_700_010_000.0))
    assert res["mode"] == "incident" and res["windows"] >= 1
    _send(client, tok_b, room_b.frames(24, "empty", seed=5, t0=1_700_010_000.0))

    state = client.get(f"/api/incidents/{incident_id}/state", headers=auth).json()
    assert state["mode"] == "change"
    zones = {z["name"]: z for z in state["zones"]}
    assert zones["301호"]["state"] == "detected"
    assert zones["301호"]["priority"] == 1
    assert zones["301호"]["evidence"]
    assert zones["302호"]["state"] == "not_detected"
    assert any(e["kind"] == "first_detection" for e in state["timeline"])

    # 지휘관 결정
    assert client.post(f"/api/incidents/{incident_id}/decisions", json={"zone_id": za["id"], "action": "rescued"}, headers=auth).status_code == 400
    ok = client.post(f"/api/incidents/{incident_id}/decisions", json={"zone_id": za["id"], "action": "rescued", "people_count": 1}, headers=auth)
    assert ok.status_code == 201
    state = client.get(f"/api/incidents/{incident_id}/state", headers=auth).json()
    assert state["zones"][-1]["name"] == "301호"  # 처리한 구역은 목록 아래로
    assert state["summary"]["rescued_people"] == 1

    # 서브캐리어 수가 다른 프레임은 버리고 기록을 남긴다
    weird = [(1_700_020_000.0 + i * 0.02, -45.0, [1, 2] * 32) for i in range(10)]
    _send(client, tok_b, weird)
    state = client.get(f"/api/incidents/{incident_id}/state", headers=auth).json()
    assert any(e["kind"] == "frame_mismatch" for e in state["timeline"])

    report = client.get(f"/api/incidents/{incident_id}/report", headers=auth).json()
    assert report["windows_analyzed"] > 0 and report["rescued_people"] == 1

    closed = client.post(f"/api/incidents/{incident_id}/close", headers=auth).json()
    assert closed["status"] == "closed"
    assert client.post(f"/api/incidents/{incident_id}/decisions", json={"zone_id": zb["id"], "action": "cleared"}, headers=auth).status_code == 409
    frozen = client.get(f"/api/incidents/{incident_id}/state", headers=auth).json()
    assert frozen["summary"]["frozen"] is True

    # 종료 뒤에는 다시 평상시
    assert _send(client, tok_a, room_a.frames(2, "empty", seed=9))["mode"] == "idle"
    with SessionLocal() as db:
        assert db.query(Inference).filter(Inference.incident_id == incident_id).count() == report["windows_analyzed"]


def test_manual_incident_and_uncalibrated_node(client, auth):
    bid = client.post("/api/buildings", json={"name": "보정 안 한 건물"}, headers=auth).json()["id"]
    z = client.post(f"/api/buildings/{bid}/zones", json={"name": "A", "polygon": [[0, 0], [1, 0], [1, 1]]}, headers=auth).json()
    rx = client.post(f"/api/buildings/{bid}/nodes", json={"name": "RX", "role": "rx", "x": 0.5, "y": 0.5, "zone_id": z["id"]}, headers=auth).json()
    inc = client.post(f"/api/buildings/{bid}/incidents", json={"note": "훈련"}, headers=auth)
    assert inc.status_code == 201
    res = _send(client, rx["token"], SyntheticRoom(seed=2).frames(4, "empty"))
    assert res["reason"] == "uncalibrated"
    state = client.get(f"/api/incidents/{inc.json()['id']}/state", headers=auth).json()
    assert state["zones"][0]["state"] == "uncalibrated"


def test_calibration_finishes_on_read_with_consistent_flags(client, auth):
    """보정 시간이 지난 뒤 첫 조회에서 '보정 끝'과 노드의 보정 여부가 함께 맞아야 한다."""
    from datetime import datetime, timedelta, timezone
    from app.models import CalibrationRun

    bid = client.post("/api/buildings", json={"name": "보정 시간 확인"}, headers=auth).json()["id"]
    z = client.post(f"/api/buildings/{bid}/zones", json={"name": "A", "polygon": [[0, 0], [1, 0], [1, 1]]}, headers=auth).json()
    rx = client.post(f"/api/buildings/{bid}/nodes", json={"name": "RX", "role": "rx", "x": 0.5, "y": 0.5, "zone_id": z["id"]}, headers=auth).json()
    client.post(f"/api/buildings/{bid}/calibration/start", json={"duration_seconds": 60}, headers=auth)
    _send(client, rx["token"], SyntheticRoom(seed=21).frames(40, "empty", seed=21))
    with SessionLocal() as db:
        run = db.query(CalibrationRun).filter(CalibrationRun.building_id == bid).one()
        run.ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    detail = client.get(f"/api/buildings/{bid}", headers=auth).json()
    assert detail["calibration"]["status"] == "done"
    assert detail["nodes"][0]["calibrated"] is True


def test_plan_upload_validates_type(client, auth):
    bid = client.post("/api/buildings", json={"name": "평면도"}, headers=auth).json()["id"]
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    ok = client.put(f"/api/buildings/{bid}/plan", files={"file": ("p.png", png, "image/png")},
                    data={"width": "800", "height": "600"}, headers=auth)
    assert ok.status_code == 200 and ok.json()["has_plan"]
    got = client.get(f"/api/buildings/{bid}/plan", headers=auth)
    assert got.status_code == 200 and got.content == png
    bad = client.put(f"/api/buildings/{bid}/plan", files={"file": ("p.txt", b"hello", "text/plain")},
                     data={"width": "800", "height": "600"}, headers=auth)
    assert bad.status_code == 415
