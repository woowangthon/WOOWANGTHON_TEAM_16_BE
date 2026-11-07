"""노드별 빈 공간 기준선.

보정 때 모은 빈 공간 프레임으로
  - 서브캐리어별 평균 진폭(mu)과 쓸 서브캐리어(mask)
  - 특징 8개의 중앙값·MAD
  - 변화 점수의 임계값 tau(빈 공간 창의 99백분위)
를 정한다. 이후 모든 판단은 이 기준선 대비 z-점수로 한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .features import FEATURE_NAMES, N_FEATURES, context_slice, iter_windows, window_features

MIN_BASELINE_WINDOWS = 8
Z_CLIP = (-5.0, 25.0)
# 특징별 척도 하한. RSSI는 1 dB 단위 정수라 보정 내내 같은 값이면 MAD가 0이 되고 척도가 0.001 dB까지 내려간다.
# 그러면 2초 창(100~170프레임)에서 한 프레임만 1 dB 깜빡여도 z가 6~10으로 튀어, 문턱이 6~9로 오르고
# 빈 방에서도 점수가 2씩 튄다(11/7 RX-301·RX-COR). 0.05 dB면 한 프레임 깜빡임은 z 0.1~0.2이고,
# 창 전체가 1 dB 바뀌면 z 20이라 사람이 길을 가리는 변화는 그대로 잡힌다.
RSSI_SCALE_FLOOR_DB = 0.05
_SCALE_FLOOR = np.array([RSSI_SCALE_FLOOR_DB if name == "rssi_shift" else 0.0 for name in FEATURE_NAMES])


class BaselineError(ValueError):
    pass


@dataclass
class Baseline:
    mu: np.ndarray
    mask: np.ndarray
    rssi_ref: float
    fps: float
    med: np.ndarray
    mad: np.ndarray
    tau: float
    n_windows: int
    n_frames: int

    @property
    def n_subcarriers(self) -> int:
        return int(self.mu.shape[0])

    def zscore(self, f: np.ndarray) -> np.ndarray:
        eps = 1e-3 + 0.05 * np.abs(np.nan_to_num(self.med))
        scale = np.maximum(1.4826 * np.nan_to_num(self.mad) + eps, _SCALE_FLOOR)
        z = (f - self.med) / scale
        z = np.where(np.isfinite(z), z, 0.0)
        return np.clip(z, *Z_CLIP)

    @staticmethod
    def change_score(z: np.ndarray) -> float:
        pos = np.maximum(z, 0.0)
        return float(math.sqrt(float(np.mean(pos**2))))

    def p_change(self, c: float) -> float:
        scale = max(0.5, 0.25 * self.tau)
        return float(1.0 / (1.0 + math.exp(-(c - self.tau) / scale)))

    def features(self, amp, rssi, ctx_amp=None, ctx_t=None):
        return window_features(amp, rssi, self.mu, self.mask, self.rssi_ref, ctx_amp, ctx_t)

    def to_json(self) -> dict:
        def clean(a):
            return [None if not math.isfinite(float(v)) else float(v) for v in a]

        return {
            "mu": clean(self.mu),
            "mask": [bool(v) for v in self.mask],
            "rssi_ref": self.rssi_ref if math.isfinite(self.rssi_ref) else None,
            "fps": self.fps,
            "median": dict(zip(FEATURE_NAMES, clean(self.med))),
            "mad": dict(zip(FEATURE_NAMES, clean(self.mad))),
            "tau": self.tau,
            "n_windows": self.n_windows,
            "n_frames": self.n_frames,
        }

    @classmethod
    def from_json(cls, d: dict) -> Baseline:
        def arr(values):
            return np.array([math.nan if v is None else float(v) for v in values], dtype=np.float64)

        return cls(
            mu=arr(d["mu"]),
            mask=np.array(d["mask"], dtype=bool),
            rssi_ref=math.nan if d.get("rssi_ref") is None else float(d["rssi_ref"]),
            fps=float(d["fps"]),
            med=arr([d["median"].get(k) for k in FEATURE_NAMES]),
            mad=arr([d["mad"].get(k) for k in FEATURE_NAMES]),
            tau=float(d["tau"]),
            n_windows=int(d["n_windows"]),
            n_frames=int(d["n_frames"]),
        )


def fit_baseline(
    t: np.ndarray,
    rssi: np.ndarray,
    amp: np.ndarray,
    window_s: float = 2.0,
    context_s: float = 16.0,
) -> Baseline:
    if amp.ndim != 2 or amp.shape[0] < 10:
        raise BaselineError("보정 프레임이 너무 적습니다.")
    duration = float(t[-1] - t[0])
    if duration < window_s * MIN_BASELINE_WINDOWS:
        raise BaselineError(f"보정 시간이 짧습니다({duration:.0f}초). 최소 {window_s * MIN_BASELINE_WINDOWS:.0f}초가 필요합니다.")

    mu = amp.mean(axis=0)
    positive = mu[mu > 0]
    if positive.size == 0:
        raise BaselineError("진폭이 모두 0입니다. 펌웨어 설정을 확인하세요.")
    mask = mu > max(1e-6, 0.05 * float(np.median(positive)))
    if mask.sum() < 4:
        raise BaselineError("쓸 수 있는 서브캐리어가 너무 적습니다.")
    finite_rssi = rssi[np.isfinite(rssi)]
    rssi_ref = float(np.median(finite_rssi)) if finite_rssi.size else math.nan
    fps = (amp.shape[0] - 1) / duration

    tmp = Baseline(mu, mask, rssi_ref, fps, np.zeros(N_FEATURES), np.ones(N_FEATURES), 0.0, 0, int(amp.shape[0]))
    feats = []
    for i0, i1, t_end in iter_windows(t, window_s):
        c0, c1 = context_slice(t, t_end, context_s)
        f = tmp.features(amp[i0:i1], rssi[i0:i1], amp[c0:c1], t[c0:c1])
        if f is not None:
            feats.append(f)
    if len(feats) < MIN_BASELINE_WINDOWS:
        raise BaselineError("유효한 창이 부족합니다. 패킷이 제대로 들어오는지 확인하세요.")
    f_arr = np.vstack(feats)
    with np.errstate(all="ignore"):
        med = np.nanmedian(f_arr, axis=0)
        mad = np.nanmedian(np.abs(f_arr - med), axis=0)

    base = Baseline(mu, mask, rssi_ref, fps, med, mad, 0.0, len(feats), int(amp.shape[0]))
    scores = np.array([base.change_score(base.zscore(f)) for f in f_arr])
    # 빈 공간에서 1%만 넘도록. 너무 낮으면 작은 잡음에도 반응하므로 하한을 둔다.
    base.tau = float(max(2.0, np.quantile(scores, 0.99)))
    return base
