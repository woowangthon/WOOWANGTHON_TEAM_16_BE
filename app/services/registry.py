from __future__ import annotations

import threading
from pathlib import Path

from ..config import settings
from ..ml.model import NO_MODEL_CARD, PresenceModel


class ModelRegistry:
    """서버 프로세스에 하나만 두는 모델 보관소."""

    def __init__(self, path: Path):
        self.path = path
        self.model: PresenceModel | None = None
        self._lock = threading.Lock()

    def load(self) -> None:
        with self._lock:
            self.model = PresenceModel.load(self.path)

    @property
    def mode(self) -> str:
        return "person" if self.model is not None else "change"

    def card(self) -> dict:
        if self.model is None:
            return dict(NO_MODEL_CARD)
        card = dict(self.model.card)
        card.setdefault("status", "trained")
        return card


registry = ModelRegistry(settings.model_path)
