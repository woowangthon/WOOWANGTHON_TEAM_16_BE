"""테스트 전용 합성 CSI 생성기.

실제 측정값이 아니다. 코드가 끝까지 도는지, 계산이 의도한 방향으로 움직이는지만 확인한다.
화면 시연, 발표 자료, 모델 성능 수치에는 절대 쓰지 않는다.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np

N_SUB = 64
NULLS = [0, *range(27, 38)]


class SyntheticRoom:
    def __init__(self, seed: int = 1) -> None:
        rng = np.random.default_rng(seed)
        k = np.arange(N_SUB)
        h = np.zeros(N_SUB, dtype=complex)
        for _ in range(6):
            amp = rng.uniform(3, 12)
            delay = rng.uniform(0, 0.3)
            phase = rng.uniform(0, 2 * np.pi)
            h += amp * np.exp(1j * (phase - 2 * np.pi * k * delay))
        h[NULLS] = 0
        self.h = h
        self.k = k
        self.person_delay = rng.uniform(0.05, 0.25)

    def frames(self, seconds: float, condition: str = "empty", fps: float = 50.0, t0: float = 1_700_000_000.0,
               seed: int = 0) -> list[tuple[float, float, list[int]]]:
        rng = np.random.default_rng(seed)
        n = int(seconds * fps)
        t = t0 + np.cumsum(rng.uniform(0.8, 1.2, n) / fps)
        pamp = np.abs(self.h).mean() * 0.35
        k = self.k
        out = []
        for ti in t:
            h = self.h.copy()
            person = condition in {"still", "lying", "standing", "sitting", "moving", "walking", "behind_wall"}
            if condition in {"still", "lying", "standing", "sitting", "behind_wall"}:
                scale = 0.5 if condition == "behind_wall" else 1.0
                breath = 0.4 * np.sin(2 * np.pi * 0.25 * ti)
                h = h + scale * pamp * np.exp(1j * (-2 * np.pi * k * self.person_delay + breath))
            elif condition in {"moving", "walking"}:
                d = self.person_delay + 0.05 * np.sin(2 * np.pi * 0.3 * ti)
                h = h + pamp * np.exp(1j * (-2 * np.pi * k * d + 2 * np.pi * 1.2 * ti))
            elif condition == "fan":
                h = h + 0.08 * np.abs(self.h).mean() * np.exp(1j * (-2 * np.pi * k * 0.1 + 2 * np.pi * 6 * ti))
            noise = rng.normal(0, 0.5, N_SUB) + 1j * rng.normal(0, 0.5, N_SUB)
            hh = h + noise
            hh[NULLS] = 0
            raw = np.empty(2 * N_SUB)
            raw[0::2] = np.round(hh.imag)
            raw[1::2] = np.round(hh.real)
            raw = np.clip(raw, -127, 127).astype(int)
            rssi = -45.0 + rng.normal(0, 1.0) + (-2.0 if person else 0.0)
            out.append((float(ti), float(round(rssi)), raw.tolist()))
        return out


def write_session(folder: Path, session_id: str, frames, meta: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"{session_id}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["t", "rssi", "data"])
        for t, rssi, raw in frames:
            w.writerow([f"{t:.4f}", rssi, "[" + ",".join(str(v) for v in raw) + "]"])
    (folder / f"{session_id}.json").write_text(json.dumps({"session_id": session_id, **meta}, ensure_ascii=False),
                                              encoding="utf-8")
