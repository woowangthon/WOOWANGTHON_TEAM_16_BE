"""2단계 사람 판별 모델(학습 명령이 만든 파일)을 읽는다.

모델 파일이 없으면 서비스는 1단계(기준선 대비 변화)만으로 돌고,
화면 문구도 '사람 기척'이 아니라 '신호 변화'로 바뀐다.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import joblib
import numpy as np

from .features import FEATURE_NAMES

log = logging.getLogger("watchmate")

NO_MODEL_CARD = {
    "status": "no_model",
    "summary": "학습된 사람 판별 모델이 없습니다. 기준선 대비 변화(1단계)만 표시합니다.",
    "features": list(FEATURE_NAMES),
    "limitations": [
        "1단계는 '빈 공간과 다르다'만 알려 주며, 그 원인이 사람인지는 판별하지 않습니다.",
        "기척이 감지되지 않았다고 사람이 없다는 뜻은 아닙니다.",
    ],
}


class PresenceModel:
    def __init__(self, clf, features: list[str], card: dict):
        if list(features) != list(FEATURE_NAMES):
            raise ValueError("모델의 특징 목록이 현재 코드와 다릅니다. 다시 학습하세요.")
        self.clf = clf
        self.card = card

    @property
    def version(self) -> str:
        return str(self.card.get("model_version", "unknown"))

    def predict(self, z: np.ndarray) -> float:
        p = self.clf.predict_proba(np.asarray(z, dtype=np.float64).reshape(1, -1))[0, 1]
        return float(min(1.0, max(0.0, p)))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"clf": self.clf, "features": list(FEATURE_NAMES), "card": self.card}, path)
        card_path = path.with_suffix(".card.json")
        card_path.write_text(json.dumps(self.card, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> PresenceModel | None:
        # 우리 학습 명령이 만든 파일만 넣는다(joblib은 pickle 기반이라 남이 준 파일을 읽으면 안 된다).
        if not path.exists():
            return None
        try:
            blob = joblib.load(path)
            return cls(blob["clf"], blob["features"], blob["card"])
        except Exception as exc:  # 파일 손상, 버전 불일치
            log.error("모델 파일을 읽지 못했습니다: %s", exc)
            return None
