"""사람 판별 모델 학습 없이 1단계 신호 변화의 실제 입장 성능을 평가한다."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from ..services.detection import DETECTION_SOURCES
from .baseline import BaselineError
from .event_validation import evaluate_validation_events
from .sessions import load_sessions
from .train import _empty_split, _fit_day_baseline


def evaluate(data_dir: Path, events_path: Path, *, source: str = "measurement",
             timeout_seconds: float = 30.0) -> dict:
    sessions, notes = load_sessions(data_dir)
    if not sessions:
        raise ValueError("평가할 수집 세션이 없습니다.")
    groups = defaultdict(list)
    for session in sessions:
        groups[(session.node, session.day)].append(session)
    baselines, cutoffs = {}, {}
    for key, group in groups.items():
        first, remaining = _empty_split(group)
        if first is None:
            notes.append(f"{key}: 빈 방 기준선이 없어 판단 불가로 평가합니다.")
            continue
        cut = remaining[0][1] if remaining and remaining[0][0] is first else None
        cutoffs[first.session_id] = cut
        try:
            baselines[key] = _fit_day_baseline(first, cut)
        except BaselineError as exc:
            notes.append(f"{key}: 기준선 실패 — {exc}")
    report, _ = evaluate_validation_events(sessions, baselines, {}, events_path, cutoffs,
                                           source=source, timeout_seconds=timeout_seconds)
    report["analysis_scope"] = "stage1_only"
    report["model_validation"] = "not_requested"
    report["by_source"] = {key: {"stage1_only": value["stage1_only"]}
                           for key, value in report["by_source"].items()}
    report["status"] = report["by_source"][source]["stage1_only"]["status"]
    report["notes"] = notes
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="실제 입장 시각부터 신호 변화의 반응을 평가")
    parser.add_argument("--data", required=True, type=Path, help="수집 세션 폴더")
    parser.add_argument("--events", required=True, type=Path, help="관측자가 기록한 입장·퇴장 주석 JSON")
    parser.add_argument("--out", required=True, type=Path, help="사건별 결과 JSON 저장 경로")
    parser.add_argument("--evaluation-source", choices=DETECTION_SOURCES, default="measurement")
    parser.add_argument("--event-timeout-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        report = evaluate(args.data, args.events, source=args.evaluation_source,
                          timeout_seconds=args.event_timeout_seconds)
    except ValueError as exc:
        parser.error(str(exc))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(f"실제 입장 기준 평가 · {report['timeout_seconds']:g}초 이내 감지 · 신호 변화 1단계")
    for source, value in report["by_source"].items():
        result = value["stage1_only"]
        if result["status"] == "unavailable":
            print(f"[{source}] 산출 불가: {result['reason']}")
            continue
        counts = result["counts"]
        print(f"[{source}] 전체 {counts['events']} · 제시간 {counts['timely']} · 늦음 {counts['late']} · "
              f"미탐 {counts['missed']} · 무판단 {counts['unjudged']} · 관찰 부족 {counts['censored']} · "
              f"입장 전 감지 지속 {counts['preexisting_detection']}")
        latency = result["median_seconds_to_detect_successes"]
        latency_text = "산출 불가(감지된 사건 없음)" if latency is None else f"{latency:g}초"
        if result["entry_response_status"] == "unavailable":
            print("  입장 반응 산출 불가: 관측 범위 안의 실제 입장 기록이 없습니다.")
        elif result["entry_response_status"] == "partial":
            print(f"  입장 반응 부분 평가: 관측 범위 안 {result['n_entry_evaluable']}건 / "
                  f"관측 시작 전 입장 {result['n_entry_before_observation']}건")
        print(f"  감지된 {result['n_detected']}건만의 지연 중앙값: {latency_text}")
        stable = result["stable"]
        hold_text = "산출 불가" if stable["hold_ratio"] is None else f"{stable['hold_ratio']:.1%}"
        coverage_text = "산출 불가" if stable["coverage"] is None else f"{stable['coverage']:.1%}"
        print(f"  자세 유지 관측 {stable['total_s']:g}초 / 감지 유지 {stable['detected_s']:g}초 / "
              f"유지율 {hold_text} / 유효 판단 coverage {coverage_text}")
        empty = result["empty"]
        rate = empty["episodes_per_valid_hour"]
        rate_text = "산출 불가" if rate is None else f"{rate:g}회"
        print(f"  빈 방 전체 {empty['total_s']:g}초 / 유효 판단 {empty['valid_s']:g}초 / "
              f"연속 경보 {empty['episodes']}회 / 유효시간 1시간당 {rate_text}")
    print(f"결과 저장: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
