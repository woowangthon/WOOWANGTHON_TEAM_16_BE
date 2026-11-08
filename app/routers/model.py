from __future__ import annotations

from fastapi import APIRouter

from ..config import settings
from ..services.detection import DETECTION_SOURCES, EMA_ALPHA, RECENT_ROWS, DetectionSource, threshold_dict
from ..services.registry import registry

router = APIRouter(prefix="/api/model", tags=["model"])


@router.get("/card")
def model_card(source: DetectionSource = "manual"):
    """AI 검증 화면이 그대로 보여 주는 모델 카드. 숫자는 학습 명령이 만든 파일에서만 나온다."""
    card = registry.card()
    card["mode"] = registry.mode
    card["runtime"] = {
        "window_seconds": settings.window_seconds,
        "context_seconds": settings.context_seconds,
        "stale_seconds": settings.stale_seconds,
        "source": source,
        "thresholds": threshold_dict(source, registry.model is not None),
        "thresholds_by_source": {key: threshold_dict(key, registry.model is not None) for key in DETECTION_SOURCES},
        "smoothing": {"method": "ema", "alpha": EMA_ALPHA, "recent_windows": RECENT_ROWS},
    }
    return card
