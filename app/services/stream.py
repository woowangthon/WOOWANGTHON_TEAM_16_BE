"""노드에서 들어온 CSI 프레임을 처리한다.

- 평상시: 프레임을 버리고 노드 접속 시각만 갱신한다.
- 보정 중: 메모리에 모았다가 보정이 끝나면 노드별 기준선을 만든다.
- 화재 상황 중: 2초 창마다 특징 → z-점수 → 변화 확률(1단계) → 사람 확률(2단계)을 계산해 저장한다.

창 버퍼는 메모리에 있으므로 서버는 프로세스 하나(uvicorn 워커 1개)로 띄운다.
"""

from __future__ import annotations

import bisect
import logging
import math
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..ml.baseline import Baseline, BaselineError, fit_baseline
from ..ml.csi import Frame, frames_to_arrays
from ..ml.features import FEATURE_NAMES, MIN_WINDOW_QUALITY, context_slice, window_quality
from ..models import Baseline as BaselineRow
from ..models import CalibrationRun, Event, Incident, Inference, Node, Zone, utcnow
from .detection import RECENT_ROWS, detection_thresholds, smoothed_probability
from .registry import registry

log = logging.getLogger("watchmate")

def detection_started_at(db: Session, incident_id: int) -> datetime | None:
    """실측에서 경보로 전환한 뒤에는 새 운영 판단 구간만 사용한다."""
    return db.scalar(select(Event.created_at).where(Event.incident_id == incident_id,
                                                   Event.kind == "detection_reset")
                     .order_by(Event.id.desc()).limit(1))


def recent_probabilities(db, incident_id, node_id):
    query = select(Inference).where(Inference.incident_id == incident_id, Inference.node_id == node_id)
    since = detection_started_at(db, incident_id)
    if since is not None:
        query = query.where(Inference.t >= since)
    rows = db.scalars(query.order_by(Inference.id.desc()).limit(RECENT_ROWS)).all()
    return list(reversed(rows))

CALIB_MAX_SECONDS = 660.0
MAX_CATCH_UP_SECONDS = 600.0


class CalibrationError(RuntimeError):
    pass


def as_aware(dt: datetime) -> datetime:
    # SQLite는 시간대 정보를 버리고 돌려준다. 저장은 항상 UTC로 하므로 UTC로 붙인다.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class _NodeBuffer:
    def __init__(self) -> None:
        self.t: deque[float] = deque()
        self.rssi: deque[float] = deque()
        self.amp: deque[np.ndarray] = deque()
        self.next_end: float | None = None
        self.incident_id: int | None = None
        self.mismatch_noted = False

    def reset(self, incident_id: int) -> None:
        self.t.clear()
        self.rssi.clear()
        self.amp.clear()
        self.next_end = None
        self.incident_id = incident_id
        self.mismatch_noted = False


@dataclass
class _CalibrationInput:
    node_id: int
    name: str
    restart_id: int | None
    frames: list[Frame]


@dataclass
class _CalibrationJob:
    run_id: int
    building_id: int
    nodes: list[_CalibrationInput]
    done: threading.Event = field(default_factory=threading.Event)


class StreamHub:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._buffers: dict[int, _NodeBuffer] = {}
        self._calib: dict[int, list[Frame]] = {}
        self._calib_live: dict[int, dict] = {}  # 보정 중 화면에 보일 값(2초 구간별 흔들림·신호 세기)
        self._baseline_cache: dict[int, tuple[int, Baseline]] = {}
        self._first_detect_seen: set[tuple[int, int]] = set()
        # 마무리한 보정 id. 같은 보정을 두 요청이 차례로 마무리하면 두 번째가 빈 프레임으로 결과를 덮어썼다(11/7).
        self._finished_calibrations: set[int] = set()
        self._finishing_calibrations: dict[int, _CalibrationJob] = {}

    # ---- 조회 도우미 -------------------------------------------------
    @staticmethod
    def active_incident(db: Session, building_id: int) -> Incident | None:
        return db.scalar(
            select(Incident)
            .where(Incident.building_id == building_id, Incident.status == "active")
            .order_by(Incident.id.desc())
            .execution_options(populate_existing=True)
        )

    def lifecycle_lock(self):
        """보정 결과 반영과 건물 상황 전환을 같은 짧은 보호 구간에서 처리한다."""
        return self._lock

    @staticmethod
    def _running_calibration(db: Session, building_id: int) -> CalibrationRun | None:
        return db.scalar(
            select(CalibrationRun)
            .where(CalibrationRun.building_id == building_id, CalibrationRun.status == "running")
            .order_by(CalibrationRun.id.desc())
            .execution_options(populate_existing=True)
        )

    def active_calibration(self, db: Session, building_id: int) -> CalibrationRun | None:
        run = self._running_calibration(db, building_id)
        if run is not None and as_aware(run.ends_at) <= utcnow():
            # 자동 마무리는 조회/수집 요청을 붙잡지 않는다. 계산에는 별도 세션을 쓴다.
            self.finish_calibration(db, run, background=True)
            db.refresh(run)
            if run.status != "running":
                return None
        return run

    def baseline_for(self, db: Session, node_id: int) -> Baseline | None:
        row = db.scalar(
            select(BaselineRow)
            .where(BaselineRow.node_id == node_id, BaselineRow.active.is_(True))
            .order_by(BaselineRow.id.desc())
        )
        if row is None:
            self._baseline_cache.pop(node_id, None)
            return None
        cached = self._baseline_cache.get(node_id)
        if cached and cached[0] == row.id:
            return cached[1]
        base = Baseline.from_json(row.stats)
        self._baseline_cache[node_id] = (row.id, base)
        return base

    @staticmethod
    def restarted_since_baseline(db: Session, node_id: int, cutoff: datetime | None = None) -> bool:
        """지금 기준선을 만든 뒤 노드가 다시 켜졌는가.

        노드는 켜진 뒤 처음 패킷 100개로 이득(신호를 키우는 정도) 기준을 정한다. 다시 켜지면 그 기준이
        달라질 수 있어, 방이 그대로여도 신호 크기가 기준선과 어긋난다. 다시 보정하기 전까지는 판단하지 않는다.
        cutoff가 있으면 그 시각까지의 재시작만 본다(종료된 상황의 판단 보존).
        """
        made = db.scalar(select(BaselineRow.created_at)
                         .where(BaselineRow.node_id == node_id, BaselineRow.active.is_(True))
                         .order_by(BaselineRow.id.desc()))
        if made is None:
            return False
        restarts = select(Event.created_at).where(Event.node_id == node_id, Event.kind == "node_restart")
        if cutoff is not None:
            restarts = restarts.where(Event.created_at <= cutoff)
        last = db.scalar(restarts.order_by(Event.id.desc()))
        return last is not None and as_aware(last) > as_aware(made)

    def _note_restart(self, db: Session, node: Node, calibrating: bool) -> None:
        incident = self.active_incident(db, node.building_id)
        if calibrating:
            self._calib.pop(node.id, None)  # 다시 켜지기 전 신호는 크기 기준이 달라 기준선에 섞지 않는다
            self._calib_live.pop(node.id, None)
            message = f"{node.name}: 보정 중에 보드가 다시 켜졌습니다. 그 전까지 모은 신호는 버리고 다시 모읍니다."
        else:
            message = (f"{node.name}: 보드가 다시 켜졌습니다. 신호 크기 기준이 바뀌었을 수 있어, "
                       "다시 보정할 때까지 이 노드는 판단하지 않습니다.")
        db.add(Event(building_id=node.building_id, incident_id=None if incident is None else incident.id,
                     zone_id=node.zone_id, node_id=node.id, kind="node_restart", message=message))
        db.flush()  # 세션이 자동 flush를 하지 않아, 바로 뒤 restarted_since_baseline이 이 기록을 보게 한다

    # ---- 수신 --------------------------------------------------------
    def ingest(self, db: Session, node: Node, frames: list[Frame], source: str = "live",
               restarted: bool = False) -> dict:
        # 이 호출을 잠금 안에서 하면 RLock의 바깥 잠금이 계산 동안 남는다.
        self.active_calibration(db, node.building_id)
        now = utcnow()
        with self._lock:
            node.last_seen_at = now
            if frames and math.isfinite(frames[-1].rssi):
                node.last_rssi = frames[-1].rssi

            run = self._running_calibration(db, node.building_id)
            if restarted:
                self._note_restart(db, node, calibrating=run is not None)
            if run is not None:
                if run.id in self._finishing_calibrations:
                    db.commit()
                    return {"mode": "calibrating", "accepted": 0, "reason": "processing"}
                buf = self._calib.setdefault(node.id, [])
                buf.extend(frames)
                if len(buf) > 2 and buf[-1].t - buf[0].t > CALIB_MAX_SECONDS:
                    cut = buf[-1].t - CALIB_MAX_SECONDS
                    self._calib[node.id] = [f for f in buf if f.t >= cut]
                db.commit()
                return {"mode": "calibrating", "accepted": len(frames)}

            incident = self.active_incident(db, node.building_id)
            if incident is None:
                # 평상시에는 CSI를 저장하지 않는다.
                db.commit()
                return {"mode": "idle", "accepted": 0}

            base = self.baseline_for(db, node.id)
            if base is None:
                db.commit()
                return {"mode": "incident", "accepted": 0, "reason": "uncalibrated"}
            if self.restarted_since_baseline(db, node.id):
                db.commit()
                return {"mode": "incident", "accepted": 0, "reason": "restarted"}

            buf = self._buffers.setdefault(node.id, _NodeBuffer())
            if buf.incident_id != incident.id:
                buf.reset(incident.id)

            good = [f for f in frames if f.amp.size == base.n_subcarriers]
            dropped = len(frames) - len(good)
            if dropped and not buf.mismatch_noted:
                buf.mismatch_noted = True
                db.add(Event(
                    building_id=node.building_id, incident_id=incident.id, node_id=node.id, kind="frame_mismatch",
                    message=f"{node.name}: 서브캐리어 수가 기준선({base.n_subcarriers})과 다른 프레임 {dropped}개를 버렸습니다. "
                            "펌웨어 설정을 바꿨다면 다시 보정하세요.",
                ))
            for f in sorted(good, key=lambda fr: fr.t):
                if buf.t and f.t <= buf.t[-1]:
                    continue  # 순서가 뒤바뀐 프레임은 버린다
                buf.t.append(f.t)
                buf.rssi.append(f.rssi)
                buf.amp.append(f.amp)

            n_windows = self._process(db, node, incident, base, buf, source, now)
            db.commit()
            return {"mode": "incident", "accepted": len(good), "windows": n_windows}

    def _process(self, db: Session, node: Node, incident: Incident, base: Baseline,
                 buf: _NodeBuffer, source: str, now: datetime) -> int:
        w, c = settings.window_seconds, settings.context_seconds
        if not buf.t:
            return 0
        last_t = buf.t[-1]
        if buf.next_end is None:
            buf.next_end = buf.t[0] + w
        if last_t - buf.next_end > MAX_CATCH_UP_SECONDS:
            buf.next_end = last_t - MAX_CATCH_UP_SECONDS

        t_arr = np.fromiter(buf.t, dtype=np.float64, count=len(buf.t))
        rssi_arr = np.fromiter(buf.rssi, dtype=np.float64, count=len(buf.rssi))
        amp_arr = np.vstack(buf.amp)
        # 노드(수집 PC) 시계를 서버 시계로 옮긴다. 묶음으로 들어와도 창 사이 간격이 유지된다.
        offset = now.timestamp() - last_t
        model = registry.model
        made = 0
        recent = None
        detect_p, _ = detection_thresholds(incident.source, model is not None)
        since = detection_started_at(db, incident.id)
        while buf.next_end <= last_t:
            end = buf.next_end
            buf.next_end += w
            i0 = int(np.searchsorted(t_arr, end - w, side="left"))
            i1 = int(np.searchsorted(t_arr, end, side="left"))
            c0, c1 = context_slice(t_arr, end, c)
            f = base.features(amp_arr[i0:i1], rssi_arr[i0:i1], amp_arr[c0:c1], t_arr[c0:c1])
            if f is None:
                continue
            z = base.zscore(f)
            score = base.change_score(z)
            p_change = base.p_change(score)
            p_person = model.predict(z) if model is not None else None
            quality = window_quality(i1 - i0, base.fps, w)
            row = Inference(
                incident_id=incident.id,
                node_id=node.id,
                zone_id=node.zone_id,
                t=datetime.fromtimestamp(end + offset, tz=timezone.utc),
                change_score=round(score, 4),
                p_change=round(p_change, 4),
                p_person=None if p_person is None else round(p_person, 4),
                z={k: round(float(v), 3) for k, v in zip(FEATURE_NAMES, z)},
                quality=round(quality, 3),
                source=source,
            )
            current_phase = since is None or row.t >= as_aware(since)
            if (current_phase and node.zone_id is not None
                    and (incident.id, node.zone_id) not in self._first_detect_seen):
                # 모든 모드에서 화면과 같은 평활 확률로 첫 감지를 기록한다.
                if recent is None:
                    recent = deque(recent_probabilities(db, incident.id, node.id), maxlen=RECENT_ROWS)
                recent.append(row)
                p = smoothed_probability(recent)
                if row.quality >= MIN_WINDOW_QUALITY and p is not None and p >= detect_p:
                    self._note_first_detection(db, incident, node, p, model is not None)
            db.add(row)
            made += 1

        keep_from = (buf.next_end or last_t) - w - c
        while buf.t and buf.t[0] < keep_from:
            buf.t.popleft()
            buf.rssi.popleft()
            buf.amp.popleft()
        return made

    def _note_first_detection(self, db: Session, incident: Incident, node: Node, p: float, person_mode: bool) -> None:
        key = (incident.id, node.zone_id)
        if key in self._first_detect_seen:
            return
        query = select(Event.id).where(Event.incident_id == incident.id, Event.zone_id == node.zone_id,
                                       Event.kind == "first_detection")
        since = detection_started_at(db, incident.id)
        if since is not None:
            query = query.where(Event.created_at >= since)
        exists = db.scalar(query)
        self._first_detect_seen.add(key)
        if exists:
            return
        zone = db.get(Zone, node.zone_id)
        what = "사람 기척" if person_mode else "신호 변화"
        db.add(Event(
            building_id=incident.building_id, incident_id=incident.id, zone_id=node.zone_id, node_id=node.id,
            kind="first_detection",
            message=f"{zone.name if zone else '구역'}: {what} 감지 (확률 {p:.0%}, 노드 {node.name})",
        ))

    def forget_incident(self, incident_id: int) -> None:
        with self._lock:
            for node_id in [k for k, b in self._buffers.items() if b.incident_id == incident_id]:
                self._buffers.pop(node_id, None)
            self._first_detect_seen = {key for key in self._first_detect_seen if key[0] != incident_id}

    def reset_detection(self, db: Session, incident: Incident) -> None:
        """실측 이력은 보존하고, 경보 이후의 판단·첫 감지를 새로 시작한다."""
        with self._lock:
            self.forget_incident(incident.id)
            db.add(Event(building_id=incident.building_id, incident_id=incident.id, kind="detection_reset",
                         message="실측 판단 구간을 마치고, 경보 수신 이후 데이터로 판단을 시작합니다."))
            db.flush()

    # ---- 보정 --------------------------------------------------------
    def start_calibration(self, db: Session, building_id: int, duration_s: int) -> CalibrationRun:
        with self._lock:
            if self.active_incident(db, building_id) is not None:
                raise CalibrationError("화재 상황이 열려 있는 동안에는 보정할 수 없습니다.")
            for old in db.scalars(select(CalibrationRun).where(
                    CalibrationRun.building_id == building_id, CalibrationRun.status == "running")):
                old.status = "failed"
                old.finished_at = utcnow()
                old.result = {"_note": "새 보정이 시작돼 중단됨"}
            nodes = db.scalars(select(Node).where(Node.building_id == building_id, Node.role == "rx")).all()
            if not nodes:
                raise CalibrationError("수신(RX) 노드가 없습니다. 먼저 노드를 등록하세요.")
            for n in nodes:
                self._calib[n.id] = []
                self._calib_live.pop(n.id, None)
            run = CalibrationRun(building_id=building_id, ends_at=utcnow() + timedelta(seconds=duration_s))
            db.add(run)
            db.add(Event(building_id=building_id, kind="calibration",
                         message=f"기준선 보정 시작({duration_s}초). 이 시간 동안 공간을 비워 두세요."))
            db.commit()
            return run

    @staticmethod
    def _last_restart_id(db: Session, node_id: int) -> int | None:
        return db.scalar(select(Event.id).where(Event.node_id == node_id, Event.kind == "node_restart")
                         .order_by(Event.id.desc()).limit(1))

    def finish_calibration(self, db: Session, run: CalibrationRun, *, background: bool = False) -> CalibrationRun:
        with self._lock:
            db.refresh(run)
            if run.status != "running" or run.id in self._finished_calibrations:
                return run
            job = self._finishing_calibrations.get(run.id)
            owner = job is None
            if owner:
                nodes = db.scalars(select(Node).where(Node.building_id == run.building_id,
                                                      Node.role == "rx")).all()
                job = _CalibrationJob(run.id, run.building_id, [
                    _CalibrationInput(n.id, n.name, self._last_restart_id(db, n.id),
                                      self._calib.pop(n.id, [])) for n in nodes
                ])
                for n in nodes:
                    self._calib_live.pop(n.id, None)
                self._finishing_calibrations[run.id] = job
            bind = db.get_bind()
            # 긴 계산/대기 중에는 요청의 트랜잭션이나 DB 연결을 붙잡지 않는다.
            db.commit()

        if owner:
            if background:
                threading.Thread(target=self._calculate_calibration, args=(job, bind),
                                 name=f"calibration-{job.run_id}", daemon=True).start()
            else:
                self._calculate_calibration(job, bind)
        elif not background:
            # 같은 보정의 중복 종료도 전역 잠금 밖에서 기다린다.
            job.done.wait()
        db.refresh(run)
        return run

    def _calculate_calibration(self, job: _CalibrationJob, bind) -> None:
        results: dict[str, dict] = {}
        baselines: dict[int, Baseline] = {}
        try:
            # 배열 변환/SVD를 포함한 모든 계산은 잠금과 DB 세션 밖에서 실행한다.
            for item in job.nodes:
                entry = {"node": item.name, "frames": len(item.frames)}
                results[str(item.node_id)] = entry
                if not item.frames:
                    entry.update(ok=False, error="보정 시간 동안 들어온 프레임이 없습니다.")
                    continue
                try:
                    t, rssi, amp = frames_to_arrays(item.frames)
                    base = fit_baseline(t, rssi, amp, settings.window_seconds, settings.context_seconds)
                except BaselineError as exc:
                    entry.update(ok=False, error=str(exc))
                    continue
                except Exception:
                    log.exception("노드 %s 기준선 계산 실패", item.node_id)
                    entry.update(ok=False, error="기준선 계산 중 오류가 발생했습니다. 다시 보정하세요.")
                    continue
                baselines[item.node_id] = base
                entry.update(ok=True, windows=base.n_windows, fps=round(base.fps, 1), tau=round(base.tau, 2),
                             subcarriers=base.n_subcarriers, subcarriers_used=int(base.mask.sum()))

            with Session(bind, autoflush=False, expire_on_commit=False) as db, self._lock:
                run = db.get(CalibrationRun, job.run_id)
                current = self._running_calibration(db, job.building_id)
                # 계산 중 새 보정이나 경보가 시작됐다면 옛 결과는 저장하지 않는다.
                if (run is None or run.status != "running" or current is None
                        or current.id != job.run_id or self.active_incident(db, job.building_id) is not None):
                    return
                ok = 0
                for item in job.nodes:
                    base = baselines.get(item.node_id)
                    if base is None:
                        continue
                    node = db.get(Node, item.node_id)
                    entry = results[str(item.node_id)]
                    if node is None or node.building_id != job.building_id or node.role != "rx":
                        entry.update(ok=False, error="계산 중 노드 설정이 변경되어 기준선을 저장하지 않았습니다.")
                    elif self._last_restart_id(db, node.id) != item.restart_id:
                        entry.update(ok=False, error="계산 중 보드가 다시 켜졌습니다. 다시 보정하세요.")
                    else:
                        self.store_baseline(db, node, base)
                        ok += 1
                run.status = "done" if ok else "failed"
                run.finished_at = utcnow()
                run.result = results
                db.add(Event(building_id=run.building_id, kind="calibration",
                             message=f"기준선 보정 {'완료' if ok else '실패'}: 노드 {ok}/{len(job.nodes)}개"))
                db.commit()
                self._finished_calibrations.add(run.id)
        except Exception:
            log.exception("보정 %s 결과 반영 실패", job.run_id)
            # 백그라운드 오류 뒤에도 영원히 진행 중으로 남기지 않는다.
            with Session(bind, expire_on_commit=False) as db, self._lock:
                run = db.get(CalibrationRun, job.run_id)
                if run is not None and run.status == "running":
                    run.status = "failed"
                    run.finished_at = utcnow()
                    run.result = {"_note": "보정 처리 중 오류가 발생했습니다. 다시 보정하세요."}
                    db.commit()
        finally:
            with self._lock:
                self._finishing_calibrations.pop(job.run_id, None)
                job.done.set()

    def store_baseline(self, db: Session, node: Node, base: Baseline) -> BaselineRow:
        db.execute(update(BaselineRow).where(BaselineRow.node_id == node.id, BaselineRow.active.is_(True))
                   .values(active=False))
        row = BaselineRow(node_id=node.id, n_windows=base.n_windows, n_frames=base.n_frames,
                          n_subcarriers=base.n_subcarriers, stats=base.to_json(), active=True)
        db.add(row)
        self._baseline_cache.pop(node.id, None)
        return row

    def calibration_progress(self, node_ids: list[int]) -> dict[int, dict]:
        with self._lock:
            out = {}
            for nid in node_ids:
                frames = self._calib.get(nid)
                live = self._calibration_live(nid, frames) if frames else None
                if frames is None:
                    frames = next((item.frames for job in self._finishing_calibrations.values()
                                   for item in job.nodes if item.node_id == nid), [])
                span = frames[-1].t - frames[0].t if len(frames) > 1 else 0.0
                out[nid] = {"frames": len(frames), "seconds": round(span, 1), "live": live}
            return out

    def _calibration_live(self, nid: int, frames: list[Frame]) -> dict | None:
        """보정 중인 보드의 지금 상태. 2초 구간마다 한 번만 계산해 쌓아 둔다(락을 잡은 채 부른다).

        흔들림 = 이웃한 프레임 사이의 진폭 모양 변화(프레임 평균 크기로 나눈 값의 차이 평균).
        이번 보정의 중간값과 견줘 '평소의 몇 배'로 보여 준다. 사람이 움직이면 커진다.
        """
        width = 2.0
        st = self._calib_live.setdefault(nid, {"t_next": frames[0].t, "wins": []})
        if st["t_next"] < frames[0].t - width:
            st["t_next"] = frames[0].t
        key = lambda f: f.t  # noqa: E731
        while frames[-1].t >= st["t_next"] + width:
            i0 = bisect.bisect_left(frames, st["t_next"], key=key)
            i1 = bisect.bisect_left(frames, st["t_next"] + width, key=key)
            st["t_next"] += width
            if i1 - i0 < 8:
                continue
            try:
                amp = np.vstack([f.amp for f in frames[i0:i1]]).astype(np.float64)
            except ValueError:  # 서브캐리어 수가 섞인 구간은 건너뛴다
                continue
            level = amp.mean(axis=1, keepdims=True)
            level[level <= 0] = 1.0
            wobble = float(np.mean(np.abs(np.diff(amp / level, axis=0))))
            rssi = [f.rssi for f in frames[i0:i1] if math.isfinite(f.rssi)]
            st["wins"].append((wobble, sum(rssi) / len(rssi) if rssi else float("nan"), (i1 - i0) / width))
        wins = st["wins"]
        if not wins:
            return None
        typical = float(np.median([w for w, _, _ in wins])) if len(wins) >= 5 else None
        rssis = [r for _, r, _ in wins if math.isfinite(r)]
        start = rssis[:5]
        ratio = (lambda w: round(w / typical, 2)) if typical else (lambda w: None)
        return {
            "windows": len(wins),
            "fps": round(wins[-1][2], 1),
            "rssi": round(rssis[-1], 1) if rssis else None,
            "rssi_start": round(sum(start) / len(start), 1) if start else None,
            "rssi_min": round(min(rssis), 1) if rssis else None,
            "rssi_max": round(max(rssis), 1) if rssis else None,
            "wobble": ratio(wins[-1][0]),
            "loud": sum(1 for w, _, _ in wins if w > 1.5 * typical) if typical else 0,
            "series": [ratio(w) for w, _, _ in wins[-60:]] if typical else [],
        }


hub = StreamHub()
