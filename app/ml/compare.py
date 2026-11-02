"""비교표: 녹화한 장면마다 '그날 빈 방 대비 신호가 얼마나 변했나'를 노드별로 보여 준다.

학습 없이 1단계(기준선 대비 변화) 계산만 쓴다. 기준선은 학습 명령과 서비스 보정과 같은 방법으로
(노드, 날짜)마다 그날 첫 빈 방 세션에서 잡는다(app/ml/train.py의 _empty_split, _fit_day_baseline).

감지가 잘 안 될 때 원인을 가르는 데 쓴다.
  - 빈 방끼리(start_empty와 end_empty)도 크게 다르다 → 기준선이 흔들리는 환경(문, 사람 왕래, 보드 이동)
  - 걷기에도 변화가 작다                               → 보드 자리(송신→수신 경로가 그 구역을 지나지 않음)
  - 걷기는 크고 누움·앉음만 작다                         → 1단계의 한계. 2단계 모델 결과를 본다
  - 옆 구역 사람·선풍기·문·열기류에 크게 반응한다         → 오경보 후보

사용법(저장소 루트에서):
    python tools/collector.py compare --data ../data/sessions/2026-11-04
    python -m app.ml.compare --data ../data/sessions/2026-11-04
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from .baseline import Baseline, BaselineError
from .features import FEATURE_LABELS_KO, FEATURE_NAMES
from .sessions import LABEL_KO, PERSON_LABELS, Session, load_sessions
from .train import _empty_split, _fit_day_baseline, _windows

# 창 하나의 변화 점수가 그날 빈 방의 99백분위(tau)를 넘으면 '변화 큰 창'으로 센다(p_change ≥ 0.5와 같음).
BIG_MOVE = {"walking", "moving"}
EMPTY_MAX = 0.20       # 빈 방인데 변화 큰 창이 이보다 많으면 기준선이 흔들리는 것
WALK_MIN = 0.50        # 걷기인데 변화 큰 창이 이보다 적으면 보드 자리를 의심
PERSON_MIN = 0.50      # 그 밖의 사람 장면이 이보다 적으면 1단계만으로는 약함
NONHUMAN_MAX = 0.30    # 옆 구역·선풍기·문·열기류가 이보다 많으면 오경보 후보


@dataclass
class Row:
    scene: str
    label: str
    seconds: float
    hz: float
    windows: int = 0
    score_med: float = float("nan")
    score_p90: float = float("nan")
    big_ratio: float = float("nan")
    top_feature: str = ""
    top_z: float = float("nan")
    status: str = ""      # 기준선 / 정상 / 확인 / 참고 / 제외
    message: str = ""


@dataclass
class Group:
    node: str
    day: str
    baseline_scene: str = ""
    baseline_seconds: float = 0.0
    tau: float = float("nan")
    rows: list[Row] = field(default_factory=list)
    note: str = ""


def _scene(s: Session) -> str:
    return str(s.meta.get("scene") or s.label)


def _judge(label: str, ratio: float) -> tuple[str, str]:
    if label == "empty":
        if ratio <= EMPTY_MAX:
            return "정상", "빈 방끼리 비슷합니다"
        return "확인", "빈 방인데 변화가 큽니다. 문 상태, 사람 왕래, 보드가 움직였는지 확인하세요"
    if label in BIG_MOVE:
        if ratio >= WALK_MIN:
            return "정상", "큰 움직임이 잘 보입니다"
        return "확인", "큰 움직임에도 변화가 작습니다. 송신→수신 경로가 이 구역을 지나도록 보드 자리를 옮겨 보세요"
    if label in PERSON_LABELS:
        if ratio >= PERSON_MIN:
            return "정상", "빈 방과 구분됩니다"
        return "참고", "1단계만으로는 약합니다. 학습 뒤 모델 카드의 조건별 결과(by_label)를 함께 보세요"
    if label == "neighbor":
        if ratio <= NONHUMAN_MAX:
            return "정상", "옆 구역 사람에 크게 반응하지 않습니다"
        return "확인", "옆 구역 사람에도 크게 반응합니다. 노드를 자기 구역 안쪽으로 옮겨 보세요"
    if ratio <= NONHUMAN_MAX:
        return "정상", "사람이 아닌 흔들림에 크게 반응하지 않습니다"
    return "참고", "사람이 아닌데 변화가 큽니다(오경보 후보). 2단계 모델이 가려야 하는 장면입니다"


def _row(s: Session, base: Baseline | None, start_after: float | None) -> Row:
    seconds = s.duration
    hz = (s.t.size - 1) / seconds if seconds > 0 else 0.0
    row = Row(_scene(s), s.label, seconds, hz)
    if base is None:
        return row
    ws = _windows(s, base, start_after)
    if ws is None:
        row.status, row.message = "제외", "서브캐리어 수가 기준선과 다릅니다(그날 펌웨어 설정이 바뀌었는지 확인)"
        return row
    scores = np.array([base.change_score(z) for z in ws.z])
    row.windows = int(scores.size)
    row.score_med = float(np.median(scores))
    row.score_p90 = float(np.percentile(scores, 90))
    row.big_ratio = float(np.mean(scores > base.tau))
    zmed = np.median(ws.z, axis=0)
    k = int(np.argmax(zmed))
    row.top_feature, row.top_z = FEATURE_NAMES[k], float(zmed[k])
    row.status, row.message = _judge(s.label, row.big_ratio)
    return row


def compare_sessions(sessions: list[Session]) -> list[Group]:
    by_key: dict[tuple[str, str], list[Session]] = defaultdict(list)
    for s in sessions:
        by_key[(s.node, s.day)].append(s)
    groups: list[Group] = []
    for (node, day), items in sorted(by_key.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        g = Group(node, day)
        items = sorted(items, key=lambda s: s.start)
        base_session, extra = _empty_split(items)
        if base_session is None:
            g.note = "빈 방(empty) 세션이 없어 비교할 기준이 없습니다. 그날 start_empty를 먼저 찍으세요."
            g.rows = [_row(s, None, None) for s in items]
            groups.append(g)
            continue
        use_until = extra[0][1] if extra and extra[0][0] is base_session else None
        try:
            base = _fit_day_baseline(base_session, use_until)
        except BaselineError as exc:
            g.note = f"기준선을 만들지 못했습니다: {exc}"
            g.rows = [_row(s, None, None) for s in items]
            groups.append(g)
            continue
        g.baseline_scene = _scene(base_session)
        g.baseline_seconds = (use_until - base_session.start) if use_until else base_session.duration
        g.tau = base.tau
        if use_until is not None:
            g.note = "빈 방 세션이 하나뿐이라 앞쪽을 기준선, 뒤쪽을 빈 방 비교에 썼습니다."
        for s in items:
            if s is base_session and use_until is None:
                r = Row(_scene(s), s.label, s.duration, (s.t.size - 1) / max(s.duration, 1e-9))
                r.status, r.message = "기준선", "그날 이 노드의 빈 방 기준입니다"
                g.rows.append(r)
                continue
            g.rows.append(_row(s, base, use_until if s is base_session else None))
        groups.append(g)
    return groups


def _fmt(v: float, spec: str) -> str:
    return "-" if not np.isfinite(v) else format(v, spec)


def format_report(groups: list[Group]) -> str:
    out: list[str] = []
    flagged: list[str] = []
    for g in groups:
        head = f"■ {g.node} · {g.day}"
        if g.baseline_scene:
            head += f" · 기준선 {g.baseline_scene}({g.baseline_seconds:.0f}초) · 변화 임계값 τ={g.tau:.1f}"
        out.append(head)
        if g.note:
            out.append(f"  {g.note}")
        out.append(f"  {'장면':<16}{'라벨':<12}{'초':>5}{'초당':>6}{'창':>5}{'변화점수':>9}{'변화 큰 창':>11}  {'판단':<6}주로 변한 것")
        for r in g.rows:
            label = LABEL_KO.get(r.label, r.label)
            # 빈 방처럼 거의 안 변한 장면은 '주로 변한 것'을 비워 둔다(2σ 미만은 잡음 수준)
            feat = (f"{FEATURE_LABELS_KO.get(r.top_feature, r.top_feature)} {r.top_z:+.1f}σ"
                    if r.top_feature and r.top_z >= 2.0 else "-")
            ratio = "-" if not np.isfinite(r.big_ratio) else f"{r.big_ratio:.0%}"
            out.append(f"  {r.scene:<16}{label:<12}{r.seconds:>5.0f}{r.hz:>6.0f}{r.windows:>5}"
                       f"{_fmt(r.score_med, '.1f'):>9}{ratio:>11}  {r.status:<6}{feat}")
            if r.status in {"확인", "제외"}:
                flagged.append(f"{g.node} {g.day} {r.scene}: {r.message}")
        out.append("")
    if flagged:
        out.append("확인할 것:")
        out += [f"  - {x}" for x in flagged]
    elif groups:
        out.append("확인할 것이 없습니다. 판단이 '참고'인 장면은 학습 뒤 모델 카드에서 다시 봅니다.")
    out.append("")
    out.append("읽는 법: '변화 큰 창'은 2초 창 중 그날 빈 방의 99백분위(τ)를 넘은 비율입니다. "
               "빈 방은 낮고 걷기는 높아야 합니다.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="녹화 장면마다 그날 빈 방 대비 변화를 노드별로 보여 준다")
    ap.add_argument("--data", required=True, help="세션 폴더(하위 폴더까지 읽음)")
    ap.add_argument("--node", action="append", default=[], help="이 노드만(여러 번 가능)")
    args = ap.parse_args(argv)
    sessions, problems = load_sessions(args.data)
    if args.node:
        sessions = [s for s in sessions if s.node in set(args.node)]
    for p in problems:
        print(f"건너뜀: {p}")
    if not sessions:
        print("읽을 세션이 없습니다. --data 폴더에 collector.py가 만든 .csv와 .json이 있는지 확인하세요.")
        return 1
    print(format_report(compare_sessions(sessions)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
