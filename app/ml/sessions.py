"""수집 도구가 저장한 세션 파일 읽기.

세션 하나 = <id>.csv(프레임) + <id>.json(메타데이터)
메타데이터 예:
{
  "session_id": "d1_rx301_lying_01",
  "node": "RX-301",          # 이 파일을 기록한 수신 노드
  "label": "lying",          # 아래 LABELS 중 하나
  "day": "2026-11-01",       # 교차검증 묶음 단위
  "zone": "301호",
  "people": 1,
  "notes": "문에서 2m, 바닥에 누움"
}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .csi import frames_to_arrays, read_session_csv

# 라벨은 '이 파일을 기록한 노드의 구역'에서 일어난 일을 뜻한다(docs/DATA_COLLECTION.md).
# 같은 시간에 옆 구역 노드가 기록한 파일은 neighbor로 붙인다.
PERSON_LABELS = {"standing", "sitting", "lying", "still", "moving", "walking", "behind_wall", "two_people"}
NONHUMAN_LABELS = {"empty", "neighbor", "fan", "door", "heat"}
LABELS = PERSON_LABELS | NONHUMAN_LABELS

LABEL_KO = {
    "empty": "빈 방",
    "standing": "서 있음",
    "sitting": "앉아 있음",
    "lying": "누워 있음",
    "still": "정지",
    "moving": "움직임",
    "walking": "걷기",
    "behind_wall": "가려진 자리",
    "two_people": "2명",
    "neighbor": "옆 구역에만 사람",
    "fan": "선풍기",
    "door": "문 여닫기",
    "heat": "열기류(드라이어)",
}


@dataclass
class Session:
    session_id: str
    path: Path
    node: str
    label: str
    day: str
    meta: dict
    t: np.ndarray = field(default_factory=lambda: np.empty(0))
    rssi: np.ndarray = field(default_factory=lambda: np.empty(0))
    amp: np.ndarray = field(default_factory=lambda: np.empty((0, 0)))

    @property
    def is_person(self) -> bool:
        return self.label in PERSON_LABELS

    @property
    def start(self) -> float:
        return float(self.t[0]) if self.t.size else 0.0

    @property
    def duration(self) -> float:
        return float(self.t[-1] - self.t[0]) if self.t.size > 1 else 0.0


def load_sessions(data_dir: str | Path) -> tuple[list[Session], list[str]]:
    data_dir = Path(data_dir)
    sessions: list[Session] = []
    problems: list[str] = []
    for meta_path in sorted(data_dir.rglob("*.json")):
        csv_path = meta_path.with_suffix(".csv")
        if not csv_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{meta_path.name}: JSON 오류 {exc}")
            continue
        label = str(meta.get("label", "")).strip()
        if label not in LABELS:
            problems.append(f"{meta_path.name}: 알 수 없는 라벨 '{label}'")
            continue
        node = str(meta.get("node", "")).strip()
        day = str(meta.get("day", "")).strip()
        if not node or not day:
            problems.append(f"{meta_path.name}: node와 day가 필요합니다.")
            continue
        t, rssi, amp = frames_to_arrays(read_session_csv(csv_path))
        if t.size < 20:
            problems.append(f"{csv_path.name}: 프레임이 너무 적습니다({t.size}).")
            continue
        sessions.append(Session(str(meta.get("session_id") or meta_path.stem), csv_path, node, label, day,
                                meta, t, rssi, amp))
    return sessions, problems
