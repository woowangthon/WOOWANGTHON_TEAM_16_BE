"""화면·첫 감지 기록·기준 안내가 함께 사용하는 판정 정책."""

from typing import Literal

from ..ml.features import MIN_WINDOW_QUALITY

DetectionSource = Literal["alarm", "manual", "measurement", "replay"]
DETECTION_SOURCES = ("alarm", "manual", "measurement", "replay")
MEASURED_SOURCES = frozenset({"measurement", "replay"})
RECENT_ROWS = 12
EMA_ALPHA = 0.5
MIN_WINDOWS_FOR_NOT_DETECTED = 3


def detection_thresholds(source: str, person_mode: bool) -> tuple[float, float]:
    """모델이 없는 실측·재생에 신호 변화용 기준을 적용한다."""
    return (0.50, 0.20) if source in MEASURED_SOURCES and not person_mode else (0.70, 0.35)


def threshold_dict(source: str, person_mode: bool) -> dict[str, float]:
    detected, possible = detection_thresholds(source, person_mode)
    return {"detected": detected, "possible": possible}


def smoothed_probability(rows) -> float | None:
    result = None
    for row in rows:
        if row.quality is not None and row.quality < MIN_WINDOW_QUALITY:
            continue
        p = row.p_person if row.p_person is not None else row.p_change
        result = p if result is None else EMA_ALPHA * p + (1 - EMA_ALPHA) * result
    return None if result is None else round(result, 3)
