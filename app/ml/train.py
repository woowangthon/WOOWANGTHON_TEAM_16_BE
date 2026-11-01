"""2단계 사람 판별 모델 학습과 검증.

사용법(backend 폴더에서):
    python -m app.ml.train --data ../data/sessions --out models/presence_model.joblib

하는 일
1. (노드, 날짜)마다 그날의 빈 방 세션으로 기준선을 만든다. 서비스의 '보정'과 같은 계산이다.
2. 나머지 세션을 2초 창으로 잘라 기준선 대비 z-점수 특징 8개를 만든다.
3. 날짜 하나를 통째로 빼고 학습해 그날로 평가한다(leave-one-day-out).
   같은 날 창을 섞어 나누면 이웃한 창끼리 거의 같아 점수가 부풀려진다.
4. 다른 날 기준선을 그대로 쓴 경우(재보정 없이)의 성능도 따로 잰다.
5. 전체 데이터로 최종 모델을 학습하고, 검증 결과를 모델 카드(JSON)로 함께 저장한다.
   AI 검증 화면은 이 카드만 그대로 보여 준다.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression

from ..config import settings
from .baseline import Baseline, BaselineError, fit_baseline
from .features import FEATURE_NAMES, context_slice, iter_windows
from .model import PresenceModel
from .sessions import LABEL_KO, PERSON_LABELS, Session, load_sessions

THRESHOLDS = {"possible": 0.35, "half": 0.5, "detected": 0.70}
MIN_BASELINE_SECONDS = 20.0


@dataclass
class WindowSet:
    z: np.ndarray        # (N, 8)
    y: np.ndarray        # (N,)
    p_change: np.ndarray  # 1단계 확률(비교용)
    label: list[str]
    session: list[str]
    t_rel: np.ndarray    # 세션 시작부터 초


def _empty_split(sessions: list[Session]) -> tuple[Session | None, list[tuple[Session, float]]]:
    """그날 빈 방 세션 중 첫 번째로 기준선을 잡고, 나머지 빈 구간을 평가용 음성으로 돌린다.
    빈 방 세션이 하나뿐이면 앞쪽 절반을 기준선, 뒤쪽 절반을 음성으로 쓴다.
    반환: (기준선 세션, [(평가에 쓸 세션, 이 시각 이후 창만 사용)])"""
    empties = sorted((s for s in sessions if s.label == "empty"), key=lambda s: s.start)
    if not empties:
        return None, []
    first = empties[0]
    rest = [(s, s.start) for s in empties[1:]]
    if not rest:
        cut = first.start + max(MIN_BASELINE_SECONDS, first.duration / 2)
        if first.t[-1] - cut >= 10:
            rest = [(first, cut)]
    return first, rest


def _fit_day_baseline(base_session: Session, use_until: float | None) -> Baseline:
    t, rssi, amp = base_session.t, base_session.rssi, base_session.amp
    if use_until is not None:
        k = int(np.searchsorted(t, use_until))
        t, rssi, amp = t[:k], rssi[:k], amp[:k]
    return fit_baseline(t, rssi, amp, settings.window_seconds, settings.context_seconds)


def _windows(session: Session, base: Baseline, start_after: float | None = None) -> WindowSet | None:
    if session.amp.shape[1] != base.n_subcarriers:
        return None
    zs, pcs, trel = [], [], []
    w, c = settings.window_seconds, settings.context_seconds
    for i0, i1, t_end in iter_windows(session.t, w):
        if start_after is not None and t_end - w < start_after:
            continue
        c0, c1 = context_slice(session.t, t_end, c)
        f = base.features(session.amp[i0:i1], session.rssi[i0:i1], session.amp[c0:c1], session.t[c0:c1])
        if f is None:
            continue
        z = base.zscore(f)
        zs.append(z)
        pcs.append(base.p_change(base.change_score(z)))
        trel.append(t_end - session.start)
    if not zs:
        return None
    n = len(zs)
    return WindowSet(np.vstack(zs), np.full(n, 1 if session.is_person else 0), np.array(pcs),
                     [session.label] * n, [session.session_id] * n, np.array(trel))


def _concat(sets: list[WindowSet]) -> WindowSet:
    return WindowSet(
        np.vstack([s.z for s in sets]),
        np.concatenate([s.y for s in sets]),
        np.concatenate([s.p_change for s in sets]),
        [x for s in sets for x in s.label],
        [x for s in sets for x in s.session],
        np.concatenate([s.t_rel for s in sets]),
    )


def build_day_windows(sessions: list[Session], baseline_from: dict | None = None) -> tuple[dict, list[str], dict]:
    """날짜별 WindowSet. baseline_from={(node, day): Baseline}을 주면 그 기준선을 쓴다(재보정 없음 실험)."""
    notes: list[str] = []
    by_key: dict[tuple[str, str], list[Session]] = defaultdict(list)
    for s in sessions:
        by_key[(s.node, s.day)].append(s)
    per_day: dict[str, list[WindowSet]] = defaultdict(list)
    baselines: dict[tuple[str, str], Baseline] = {}
    for (node, day), group in sorted(by_key.items()):
        base_session, extra_negatives = _empty_split(group)
        if base_session is None:
            notes.append(f"{node} {day}: 빈 방 세션이 없어 그날 데이터를 쓰지 않았습니다.")
            continue
        use_until = None
        if extra_negatives and extra_negatives[0][0] is base_session:
            use_until = extra_negatives[0][1]
        try:
            own = _fit_day_baseline(base_session, use_until)
        except BaselineError as exc:
            notes.append(f"{node} {day}: 기준선 실패 — {exc}")
            continue
        baselines[(node, day)] = own
        base = own if baseline_from is None else baseline_from.get((node, day), own)
        for s in group:
            if s is base_session and use_until is None:
                continue  # 기준선 전체로 쓴 세션은 평가에 넣지 않는다
            start_after = use_until if s is base_session else None
            ws = _windows(s, base, start_after)
            if ws is None:
                notes.append(f"{s.session_id}: 서브캐리어 수가 기준선과 달라 제외")
                continue
            per_day[day].append(ws)
    return {d: _concat(v) for d, v in per_day.items() if v}, notes, baselines


def _make_clf(kind: str):
    if kind == "hgb":
        return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.1, max_iter=200,
                                              class_weight="balanced", random_state=0)
    return LogisticRegression(C=1.0, class_weight="balanced", max_iter=2000)


def _metrics(y: np.ndarray, p: np.ndarray, labels: list[str]) -> dict:
    out: dict = {"n_windows": int(y.size), "n_person": int(y.sum()), "n_nonperson": int((1 - y).sum())}
    for name, th in THRESHOLDS.items():
        pred = p >= th
        tp = int(np.sum(pred & (y == 1)))
        fp = int(np.sum(pred & (y == 0)))
        fn = int(np.sum(~pred & (y == 1)))
        tn = int(np.sum(~pred & (y == 0)))
        recall = tp / (tp + fn) if tp + fn else None
        far = fp / (fp + tn) if fp + tn else None
        precision = tp / (tp + fp) if tp + fp else None
        f1 = (2 * precision * recall / (precision + recall)) if precision and recall else None
        out[name] = {"threshold": th, "recall": recall, "false_alarm_rate": far, "precision": precision,
                     "f1": f1, "tp": tp, "fp": fp, "fn": fn, "tn": tn}
    by_label = {}
    arr_labels = np.array(labels)
    for lab in sorted(set(labels)):
        m = arr_labels == lab
        hit = float(np.mean(p[m] >= THRESHOLDS["detected"]))
        by_label[lab] = {
            "label_ko": LABEL_KO.get(lab, lab),
            "n_windows": int(m.sum()),
            "is_person": lab in PERSON_LABELS,
            # 사람 조건이면 감지율(재현율), 아니면 오경보율
            "rate_at_0.70": round(hit, 4),
        }
    out["by_label"] = by_label
    return out


def _time_to_detect(ws: WindowSet, p: np.ndarray, alpha: float = 0.5) -> float | None:
    times = []
    sessions = np.array(ws.session)
    for sid in sorted(set(ws.session)):
        m = sessions == sid
        if ws.y[m][0] != 1:
            continue
        order = np.argsort(ws.t_rel[m])
        ema = None
        found = None
        for t_rel, pv in zip(ws.t_rel[m][order], p[m][order]):
            ema = pv if ema is None else alpha * pv + (1 - alpha) * ema
            if ema >= THRESHOLDS["detected"]:
                found = float(t_rel)
                break
        if found is not None:
            times.append(found)
    return float(np.median(times)) if times else None


def _round(obj):
    if isinstance(obj, float):
        return None if math.isnan(obj) else round(obj, 4)
    if isinstance(obj, dict):
        return {k: _round(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round(v) for v in obj]
    return obj


def train(data_dir: Path, out_path: Path, kind: str = "lr", source_note: str = "") -> dict:
    sessions, problems = load_sessions(data_dir)
    if not sessions:
        raise SystemExit("세션을 찾지 못했습니다. --data 경로와 파일 형식을 확인하세요.\n" + "\n".join(problems))
    per_day, notes, baselines = build_day_windows(sessions)
    notes = problems + notes
    days = sorted(per_day)
    if not days:
        raise SystemExit("학습할 창이 없습니다.\n" + "\n".join(notes))
    full = _concat([per_day[d] for d in days])
    if len(set(full.y.tolist())) < 2:
        raise SystemExit("사람 있음/없음 두 종류 데이터가 모두 있어야 합니다.")

    folds = []
    method = "leave-one-day-out"
    if len(days) >= 2:
        # 재보정 없이: 각 노드의 첫날 기준선으로 다른 날을 평가
        first_day_base: dict[tuple[str, str], Baseline] = {}
        for (node, day) in baselines:
            first = min(d for (n, d) in baselines if n == node)
            first_day_base[(node, day)] = baselines[(node, first)]
        cross_days, _, _ = build_day_windows(sessions, baseline_from=first_day_base)

        for d in days:
            train_set = _concat([per_day[x] for x in days if x != d])
            test_set = per_day[d]
            if len(set(train_set.y.tolist())) < 2:
                continue
            clf = _make_clf(kind).fit(train_set.z, train_set.y)
            p = clf.predict_proba(test_set.z)[:, 1]
            fold = {
                "held_out_day": d,
                "model": _metrics(test_set.y, p, test_set.label),
                "stage1_only": _metrics(test_set.y, test_set.p_change, test_set.label),
                "median_seconds_to_detect": _time_to_detect(test_set, p),
            }
            if d in cross_days and d != min(days):
                cs = cross_days[d]
                fold["no_recalibration"] = _metrics(cs.y, clf.predict_proba(cs.z)[:, 1], cs.label)
            folds.append(fold)
    else:
        method = "time-block (하루치 데이터뿐이라 낙관적으로 나올 수 있음)"
        sess = np.array(full.session)
        train_m = np.zeros(full.y.size, dtype=bool)
        for sid in set(full.session):
            m = np.where(sess == sid)[0]
            order = m[np.argsort(full.t_rel[m])]
            train_m[order[: int(len(order) * 0.6)]] = True
        clf = _make_clf(kind).fit(full.z[train_m], full.y[train_m])
        test_m = ~train_m
        p = clf.predict_proba(full.z[test_m])[:, 1]
        test_labels = [lab for lab, keep in zip(full.label, test_m) if keep]
        folds.append({
            "held_out_day": "같은 날 세션 뒤쪽 40%",
            "model": _metrics(full.y[test_m], p, test_labels),
            "stage1_only": _metrics(full.y[test_m], full.p_change[test_m], test_labels),
            "median_seconds_to_detect": None,
        })

    def mean_of(key: str, metric: str, th: str = "detected"):
        vals = [f[key][th][metric] for f in folds if key in f and f[key][th][metric] is not None]
        return float(np.mean(vals)) if vals else None

    summary = {
        "recall_at_0.70": mean_of("model", "recall"),
        "false_alarm_rate_at_0.70": mean_of("model", "false_alarm_rate"),
        "recall_at_0.35": mean_of("model", "recall", "possible"),
        "false_alarm_rate_at_0.35": mean_of("model", "false_alarm_rate", "possible"),
        "stage1_recall_at_0.70": mean_of("stage1_only", "recall"),
        "stage1_false_alarm_rate_at_0.70": mean_of("stage1_only", "false_alarm_rate"),
        "no_recalibration_recall_at_0.70": mean_of("no_recalibration", "recall"),
        "no_recalibration_false_alarm_rate_at_0.70": mean_of("no_recalibration", "false_alarm_rate"),
    }

    final = _make_clf(kind).fit(full.z, full.y)
    coefs = None
    if hasattr(final, "coef_"):
        coefs = dict(zip(FEATURE_NAMES, [float(v) for v in final.coef_[0]]))

    label_counts: dict[str, int] = defaultdict(int)
    for lab in full.label:
        label_counts[lab] += 1
    now = datetime.now(timezone.utc)
    card = _round({
        "status": "trained",
        "model_version": now.strftime("%Y%m%d-%H%M%S"),
        "created_at": now.isoformat(),
        "classifier": type(final).__name__,
        "features": list(FEATURE_NAMES),
        "coefficients": coefs,
        "window_seconds": settings.window_seconds,
        "context_seconds": settings.context_seconds,
        "data": {
            "source": source_note or "팀이 직접 수집한 ESP32 CSI 세션",
            "sessions": len(sessions),
            "days": days,
            "nodes": sorted({s.node for s in sessions}),
            "windows": int(full.y.size),
            "windows_by_label": dict(label_counts),
            "person_labels": sorted(PERSON_LABELS),
        },
        "validation": {"method": method, "folds": folds, "summary": summary},
        "notes": notes,
        "limitations": [
            "기척이 감지되지 않았다고 사람이 없다는 뜻은 아닙니다.",
            "학습한 건물·노드 배치와 다른 환경에서는 성능이 떨어질 수 있습니다. 설치 후 반드시 기준선 보정을 하세요.",
            "실제 화재(불꽃·열기류)가 신호에 주는 영향은 이 데이터로 검증하지 않았습니다.",
        ],
    })
    model = PresenceModel(final, list(FEATURE_NAMES), card)
    model.save(out_path)
    return card


def _fmt(v) -> str:
    return "-" if v is None else f"{v:.2f}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="와치메이트 사람 판별 모델 학습·검증")
    ap.add_argument("--data", required=True, type=Path, help="세션(.csv + .json) 폴더")
    ap.add_argument("--out", type=Path, default=settings.model_path, help="모델 저장 경로")
    ap.add_argument("--classifier", choices=["lr", "hgb"], default="lr")
    ap.add_argument("--source-note", default="", help="모델 카드에 적을 데이터 출처 설명")
    args = ap.parse_args(argv)

    card = train(args.data, args.out, args.classifier, args.source_note)
    v = card["validation"]
    print(f"검증 방식: {v['method']}")
    for f in v["folds"]:
        m = f["model"]["detected"]
        s1 = f["stage1_only"]["detected"]
        line = (f"  [{f['held_out_day']}] 재현율 {_fmt(m['recall'])} 오경보율 {_fmt(m['false_alarm_rate'])}"
                f" | 1단계만 재현율 {_fmt(s1['recall'])} 오경보율 {_fmt(s1['false_alarm_rate'])}")
        if "no_recalibration" in f:
            nr = f["no_recalibration"]["detected"]
            line += f" | 재보정 없이 재현율 {_fmt(nr['recall'])} 오경보율 {_fmt(nr['false_alarm_rate'])}"
        print(line)
    for n in card.get("notes", []):
        print("  참고:", n)
    print(f"모델 저장: {args.out}")
    print(f"모델 카드: {args.out.with_suffix('.card.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
