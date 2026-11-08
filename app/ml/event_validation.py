"""관측한 입장 시각을 홀드아웃 예측과 연결한다. 정답 시각을 추정하지 않는다."""

from __future__ import annotations

import math
from collections import defaultdict
from numbers import Real
from pathlib import Path

from ..config import settings
from ..services.detection import DETECTION_SOURCES, threshold_dict
from .event_annotations import load_event_annotations
from .event_evaluation import Window, evaluate_trial, summarize_trials
from .features import context_slice, iter_windows, window_quality


def _unavailable(reason: str) -> dict:
    return {"status": "unavailable", "reason": reason}


def _score_session(session, baseline, classifier, origin: float, start_after: float | None = None) -> list[Window]:
    """학습에서 제외한 저품질 창도 원래 시각에 남겨 평가의 무판단 구간을 보존한다."""
    if session.amp.shape[1] != baseline.n_subcarriers:
        return []
    result = []
    w, c = settings.window_seconds, settings.context_seconds
    begin = 0 if start_after is None else int(session.t.searchsorted(start_after))
    t, rssi, amp = session.t[begin:], session.rssi[begin:], session.amp[begin:]
    for i0, i1, end in iter_windows(t, w):
        c0, c1 = context_slice(t, end, c)
        features = baseline.features(amp[i0:i1], rssi[i0:i1], amp[c0:c1], t[c0:c1])
        if features is None:
            continue  # 서비스도 판단을 만들지 않는다. 빈 간격은 평가기가 유효시간으로 처리한다.
        z = baseline.zscore(features)
        person = None if classifier is None else float(classifier.predict_proba(z.reshape(1, -1))[0, 1])
        result.append(Window(
            node=session.node, t_end=float(end - origin),
            p_change=round(float(baseline.p_change(baseline.change_score(z))), 4),
            p_person=None if person is None else round(person, 4),
            quality=round(window_quality(i1 - i0, baseline.fps, w), 3),
        ))
    return result


def _check_calibration_occupancy(annotation, recorded, sessions_by_id, calibration_cutoffs):
    """Explicit occupancy cannot coexist with the same receiver's empty calibration."""
    trial, origin = annotation.trial, annotation.origin_epoch_s
    occupied = [(origin + event.entered_s, origin + event.exited_s) for event in trial.events]
    occupied.extend((origin + interval.start_s, origin + interval.end_s)
                    for interval in trial.settled_intervals)
    if any(not math.isfinite(value) for pair in occupied for value in pair):
        raise ValueError(f"{trial.trial_id}: 사람이 있는 구간의 절대 시각은 유한해야 합니다.")
    node_days = {(session.node, session.day) for session in recorded}
    for sid, cutoff in calibration_cutoffs.items():
        calibration = sessions_by_id.get(sid)
        if calibration is None or (calibration.node, calibration.day) not in node_days or not calibration.t.size:
            continue
        start = calibration.start
        # Full-file calibration includes its final sample. A split excludes
        # the cutoff itself, matching searchsorted(..., side='left').
        end = math.nextafter(float(calibration.t[-1]), math.inf) if cutoff is None else float(cutoff)
        if any(max(start, entered) < min(end, exited) for entered, exited in occupied):
            raise ValueError(f"{trial.trial_id}: 사람이 있었다는 관측 구간이 기준선 보정({sid})과 겹칩니다.")


def evaluate_validation_events(sessions, baselines, fold_models, annotations_path: Path | None,
                               calibration_cutoffs: dict[str, float | None], *,
                               source: str = "measurement", timeout_seconds: float = 30.0,
                               resolved_annotations=None) -> tuple[dict, dict]:
    """모델은 전체 홀드아웃 그룹 예측만, 1단계는 독립된 빈 방 보정만 사용한다."""
    if source not in DETECTION_SOURCES:
        raise ValueError(f"알 수 없는 평가 시작 방식: {source}")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, Real)
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        raise ValueError("사건 감지 제한 시간은 유한한 양수여야 합니다.")
    if annotations_path is None:
        return _unavailable("실제 입장·퇴장 시각 주석이 없어 사건 성능을 계산하지 않았습니다."), {}

    annotations = (load_event_annotations(annotations_path, sessions) if resolved_annotations is None
                   else resolved_annotations)
    by_id = {session.session_id: session for session in sessions}
    profiles = tuple(dict.fromkeys((source, "measurement", "alarm")))
    outcomes = {key: {"model": [], "stage1_only": []} for key in profiles}
    grouped = defaultdict(lambda: {key: {"model": [], "stage1_only": []} for key in profiles})
    missing = {"model": [], "stage1_only": []}
    grouped_missing = defaultdict(lambda: {"model": [], "stage1_only": []})

    for annotation in annotations:
        trial = annotation.trial
        recorded = [by_id[sid] for sid in annotation.session_ids]
        days = {session.day for session in recorded}
        if len(days) != 1:
            raise ValueError(f"{trial.trial_id}: 한 사건 시험의 세션은 같은 홀드아웃 그룹(day)에 있어야 합니다.")
        day = next(iter(days))
        _check_calibration_occupancy(annotation, recorded, by_id, calibration_cutoffs)
        for session in recorded:
            if session.session_id in calibration_cutoffs:
                cutoff = calibration_cutoffs[session.session_id]
                if cutoff is None or annotation.origin_epoch_s + trial.start_s < cutoff:
                    raise ValueError(f"{trial.trial_id}: 기준선 보정에 사용한 빈 방 구간은 사건 평가에 넣을 수 없습니다.")

        absent = [session.session_id for session in recorded if (session.node, session.day) not in baselines]
        classifier = fold_models.get(day)
        if classifier is None:
            info = {"trial_id": trial.trial_id, "zone_id": trial.zone_id,
                    "events": len(trial.events),
                    "reason": "사건 전체가 학습에서 제외된 홀드아웃 모델이 없습니다. 학습 데이터 예측으로 대체하지 않았습니다."}
            missing["model"].append(info)
            grouped_missing[day]["model"].append(info)
        windows = [window for session in recorded if (session.node, session.day) in baselines
                   for window in _score_session(session, baselines[(session.node, session.day)], classifier,
                                                 annotation.origin_epoch_s,
                                                 calibration_cutoffs.get(session.session_id))
                   if window.t_end <= trial.end_s]
        expected_nodes = [session.node for session in recorded]
        for profile in profiles:
            for kind, person_mode in (("stage1_only", False), ("model", True)):
                if person_mode and classifier is None:
                    continue
                evaluated = evaluate_trial(trial, windows, source=profile, person_mode=person_mode,
                                           timeout_s=timeout_seconds, window_s=settings.window_seconds,
                                           stale_s=settings.stale_seconds, expected_nodes=expected_nodes)
                evaluated["baseline_unavailable_sessions"] = absent
                outcomes[profile][kind].append(evaluated)
                grouped[day][profile][kind].append(evaluated)

    def pack(reports, omissions):
        result = {}
        for profile in profiles:
            result[profile] = {}
            for kind in ("model", "stage1_only"):
                trials = reports[profile][kind]
                summary = summarize_trials(trials)
                if not trials:
                    summary["reason"] = "평가할 수 있는 시험이 없습니다."
                if summary["status"] != "unavailable":
                    summary["status"] = "partial" if omissions[kind] else "available"
                summary["unavailable_trials"] = omissions[kind]
                evaluated_trials = summary["counts"]["trials"]
                annotated_trials = evaluated_trials + len(omissions[kind])
                summary["annotated_trials"] = annotated_trials
                summary["unavailable_trial_count"] = len(omissions[kind])
                summary["annotation_trial_coverage"] = evaluated_trials / annotated_trials if annotated_trials else None
                summary["stable"]["scope"] = "evaluated_trials_only" if omissions[kind] else "all_annotated_trials"
                unavailable_events = sum(item.get("events", 0) for item in omissions[kind])
                evaluated_events = summary.get("counts", {}).get("events", 0)
                annotated_events = evaluated_events + unavailable_events
                summary["annotated_events"] = annotated_events
                summary["unavailable_events"] = unavailable_events
                summary["annotation_event_coverage"] = evaluated_events / annotated_events if annotated_events else None
                if unavailable_events:
                    summary["entry_response_status"] = "partial" if summary["n_entry_evaluable"] else "unavailable"
                if summary["status"] == "partial":
                    # 일부 사건에 독립 모델이 없으면 나머지의 성공률을 전체 성공률로 부르지 않는다.
                    summary["timely_rate_evaluated_events"] = summary.get("timely_rate_all_events")
                    summary["detected_rate_evaluated_events"] = summary.get("detected_rate_all_events")
                    summary["timely_rate_all_events"] = None
                    summary["detected_rate_all_events"] = None
                summary["source"] = profile
                summary["person_mode"] = kind == "model"
                summary["thresholds"] = threshold_dict(profile, kind == "model")
                # 모델 카드에는 구간 전체 대신 사건별 결과와 집계를 보관한다.
                summary["trials"] = [{key: value for key, value in trial.items() if key != "segments"} for trial in trials]
                result[profile][kind] = summary
        return result

    available = sum(report["status"] == "evaluated" for report in outcomes[source]["stage1_only"])
    top = {
        "status": "available" if available == len(annotations) and not missing["model"] else "partial" if available else "unavailable",
        "annotation_file": Path(annotations_path).name,
        "reference": "observed_entry",
        "evaluation_unit": "one entry per trial/zone/event; multiple receivers are fused",
        "source": source, "timeout_seconds": timeout_seconds,
        "annotated_trials": len(annotations),
        "annotated_events": sum(len(annotation.trial.events) for annotation in annotations),
        "model_validation": "whole held-out group only",
        "by_source": pack(outcomes, missing),
    }
    if not available:
        top["reason"] = "실제 시각이 기록된 시험을 평가할 수 없습니다."
    per_day = {}
    for day in set(grouped) | set(grouped_missing):
        by_source = pack(grouped[day], grouped_missing[day])
        states = {report["status"] for report in by_source[source].values()}
        status = "available" if states == {"available"} else "unavailable" if states == {"unavailable"} else "partial"
        per_day[day] = {"status": status, "reference": "observed_entry", "source": source,
                        "timeout_seconds": timeout_seconds, "by_source": by_source}
    return top, per_day
