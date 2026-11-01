"""녹화한 실제 측정 세션을 서버 안에서 다시 흘려보내는 시연 기능.

심사위원은 노드가 없는 곳에서 링크만 연다. 그래서 팀이 현장에서 녹화한 CSI 세션을
라이브와 똑같은 경로(기준선 → 특징 → 판단 → 구역 상태)로 지금 이 서버에서 처리한다.
결과를 미리 정해 두는 것이 아니며, 화면에는 항상 '녹화 데이터 재생'으로 표시된다.

시나리오 폴더 구성은 sample_data/README.md 참고.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal
from ..ml.baseline import BaselineError, fit_baseline
from ..ml.csi import Frame, frames_to_arrays, read_session_csv
from ..models import Building, Event, Incident, Node, Zone, utcnow
from .stream import hub

log = logging.getLogger("watchmate")


class ReplayError(RuntimeError):
    pass


@dataclass
class ScenarioNode:
    name: str
    role: str
    x: float
    y: float
    zone: str | None = None
    calibration: Path | None = None
    stream: Path | None = None


@dataclass
class Scenario:
    id: str
    title: str
    description: str
    synthetic: bool
    recorded_at: str
    plan_image: Path | None
    plan_size: tuple[int, int]
    zones: list[dict]
    nodes: list[ScenarioNode]
    timeline: list[dict] = field(default_factory=list)
    duration_seconds: float | None = None

    def public(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "synthetic": self.synthetic,
            "allowed": (not self.synthetic) or settings.allow_synthetic_demo,
            "recorded_at": self.recorded_at,
            "duration_seconds": self.duration_seconds,
            "zones": [z["name"] for z in self.zones],
            "rx_nodes": sum(1 for n in self.nodes if n.role == "rx"),
            "timeline": self.timeline,
        }


def load_scenarios(base: Path | None = None) -> list[Scenario]:
    base = base or settings.sample_data_dir
    out: list[Scenario] = []
    if not base.exists():
        return out
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        meta_path = d / "scenario.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            nodes = []
            for n in meta["nodes"]:
                nodes.append(ScenarioNode(
                    name=n["name"], role=n["role"], x=float(n["x"]), y=float(n["y"]), zone=n.get("zone"),
                    calibration=d / n["calibration"] if n.get("calibration") else None,
                    stream=d / n["stream"] if n.get("stream") else None,
                ))
            plan = d / meta["plan_image"] if meta.get("plan_image") else None
            out.append(Scenario(
                id=str(meta.get("id") or d.name),
                title=meta["title"],
                description=meta.get("description", ""),
                synthetic=bool(meta.get("synthetic", False)),
                recorded_at=meta.get("recorded_at", ""),
                plan_image=plan if plan and plan.exists() else None,
                plan_size=tuple(meta.get("plan_size", [1000, 640])),
                zones=meta["zones"],
                nodes=nodes,
                timeline=meta.get("timeline", []),
                duration_seconds=meta.get("duration_seconds"),
            ))
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            log.error("시나리오 %s를 읽지 못했습니다: %s", d.name, exc)
    return out


class ReplayRunner:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.state: dict = {"running": False}

    def is_running(self, incident_id: int | None = None) -> bool:
        if not self.state.get("running"):
            return False
        return incident_id is None or self.state.get("incident_id") == incident_id

    def _prepare_building(self, db, sc: Scenario) -> Building:
        tag = f"scenario:{sc.id}"
        old = db.scalar(select(Building).where(Building.is_demo.is_(True), Building.address == tag))
        if old is not None:
            for inc_id in db.scalars(select(Incident.id).where(Incident.building_id == old.id)):
                hub.forget_incident(inc_id)
            db.delete(old)
            db.commit()
        b = Building(name=f"시연 · {sc.title}", address=tag, floor_label="녹화 데이터", is_demo=True)
        if sc.plan_image is not None:
            b.plan_image = sc.plan_image.read_bytes()
            b.plan_mime = "image/png" if sc.plan_image.suffix.lower() == ".png" else "image/jpeg"
            b.plan_width, b.plan_height = int(sc.plan_size[0]), int(sc.plan_size[1])
        db.add(b)
        db.flush()
        zone_ids: dict[str, int] = {}
        for z in sc.zones:
            row = Zone(building_id=b.id, name=z["name"], polygon=z["polygon"])
            db.add(row)
            db.flush()
            zone_ids[z["name"]] = row.id
        for n in sc.nodes:
            db.add(Node(building_id=b.id, name=n.name, role=n.role, x=n.x, y=n.y,
                        zone_id=zone_ids.get(n.zone) if n.zone else None))
        db.commit()
        return b

    def start(self, scenario_id: str, speed: float) -> dict:
        from ..routers.ingest import open_incident  # 순환 import 방지

        with self._lock:
            if self.is_running():
                raise ReplayError("이미 재생 중입니다. 먼저 멈추세요.")
            sc = next((s for s in load_scenarios() if s.id == scenario_id), None)
            if sc is None:
                raise ReplayError("시나리오를 찾을 수 없습니다.")
            if sc.synthetic and not settings.allow_synthetic_demo:
                raise ReplayError("합성 신호 시나리오는 개발용입니다(ALLOW_SYNTHETIC_DEMO=1일 때만 재생).")

            db = SessionLocal()
            try:
                building = self._prepare_building(db, sc)
                streams: dict[int, list[Frame]] = {}
                for sn in sc.nodes:
                    if sn.role != "rx":
                        continue
                    node = db.scalar(select(Node).where(Node.building_id == building.id, Node.name == sn.name))
                    if sn.calibration is None or sn.stream is None:
                        raise ReplayError(f"{sn.name}: calibration·stream 파일이 필요합니다.")
                    cal = read_session_csv(sn.calibration)
                    t, rssi, amp = frames_to_arrays(cal)
                    try:
                        base = fit_baseline(t, rssi, amp, settings.window_seconds, settings.context_seconds)
                    except BaselineError as exc:
                        raise ReplayError(f"{sn.name} 기준선 실패: {exc}") from exc
                    hub.store_baseline(db, node, base)
                    frames = sorted(read_session_csv(sn.stream), key=lambda f: f.t)
                    if not frames:
                        raise ReplayError(f"{sn.name}: stream 파일에 프레임이 없습니다.")
                    streams[node.id] = frames
                db.add(Event(building_id=building.id, kind="calibration",
                             message=f"녹화된 빈 공간 구간으로 기준선 생성: 노드 {len(streams)}개"))
                db.commit()
                incident, _ = open_incident(db, building, "replay", f"녹화 데이터: {sc.title}")
                t0 = min(fr[0].t for fr in streams.values())
                t_last = max(fr[-1].t for fr in streams.values())
                self.state = {
                    "running": True, "scenario_id": sc.id, "title": sc.title, "building_id": building.id,
                    "incident_id": incident.id, "speed": speed, "elapsed": 0.0,
                    "duration": round(t_last - t0, 1), "timeline": sc.timeline, "synthetic": sc.synthetic,
                }
            finally:
                db.close()

            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, args=(incident.id, streams, t0, speed), daemon=True, name="replay")
            self._thread.start()
            return dict(self.state)

    def _run(self, incident_id: int, streams: dict[int, list[Frame]], t0: float, speed: float) -> None:
        db = SessionLocal()
        idx = {nid: 0 for nid in streams}
        started = time.monotonic()
        reason = "끝까지 재생"
        try:
            while not self._stop.is_set():
                inc = db.get(Incident, incident_id)
                db.refresh(inc)
                if inc.status != "active":
                    reason = "상황이 종료되어 중단"
                    break
                cut = t0 + (time.monotonic() - started) * speed
                remaining = False
                for nid, frames in streams.items():
                    i = j = idx[nid]
                    while j < len(frames) and frames[j].t <= cut:
                        j += 1
                    if j > i:
                        node = db.get(Node, nid)
                        hub.ingest(db, node, frames[i:j], source="replay")
                        idx[nid] = j
                    if j < len(frames):
                        remaining = True
                self.state["elapsed"] = round(min(cut - t0, self.state.get("duration", 0.0)), 1)
                if not remaining:
                    break
                self._stop.wait(0.25)
            else:
                reason = "사용자가 중지"
            db.add(Event(building_id=db.get(Incident, incident_id).building_id, incident_id=incident_id,
                         kind="replay_end", message=f"녹화 데이터 재생 끝({reason})."))
            db.commit()
        except Exception as exc:  # 재생 실패가 서버를 멈추게 하지는 않는다
            log.exception("replay failed")
            try:
                db.rollback()
                inc = db.get(Incident, incident_id)
                db.add(Event(building_id=inc.building_id, incident_id=incident_id, kind="replay_error",
                             message=f"재생 중 오류: {exc}"))
                db.commit()
            except Exception:  # noqa: BLE001
                pass
        finally:
            db.close()
            self.state["running"] = False
            self.state["finished_at"] = utcnow().isoformat()

    def stop(self) -> dict:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.state["running"] = False
        return dict(self.state)


runner = ReplayRunner()
