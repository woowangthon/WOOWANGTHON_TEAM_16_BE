"""2초 창 하나에서 특징 8개를 뽑는다.

모든 특징은 노드 기준선(빈 공간 평균 진폭)으로 나눈 상대 진폭에서 계산한다.
공간마다 다른 신호 크기를 지우고 '얼마나 변했나'만 남기기 위해서다.
특징을 8개로 묶어 둔 것은 공간 고유의 반사 패턴에 과적합되지 않게 하려는 것이다.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.signal import detrend, welch

FEATURE_NAMES: tuple[str, ...] = (
    "motion_med",    # 서브캐리어별 시간 변동(상대 표준편차)의 중앙값
    "motion_p90",    # 같은 값의 상위 10% — 일부 경로만 크게 흔들릴 때
    "diff_mean",     # 연속 프레임 간 변화량 평균 — 빠른 움직임
    "pc1_ratio",     # 1주성분 분산 비율 — 몸 움직임은 여러 서브캐리어를 함께 흔든다
    "static_shift",  # 평균 진폭 모양과 기준선의 상관 감소 — 정지한 사람·물체
    "level_shift",   # 평균 진폭 크기 변화
    "rssi_shift",    # 수신 세기 변화(dB)
    "breath_ratio",  # 최근 16초 1주성분에서 0.15~0.6Hz 대역 비율 — 호흡 수준의 주기 변동
)

# 화면 근거 문장용
FEATURE_LABELS_KO: dict[str, str] = {
    "motion_med": "움직임 신호",
    "motion_p90": "국소 움직임",
    "diff_mean": "빠른 변동",
    "pc1_ratio": "동조 변화(몸 움직임 패턴)",
    "static_shift": "정적 반사 변화",
    "level_shift": "신호 세기 변화",
    "rssi_shift": "RSSI 변화",
    "breath_ratio": "호흡 대역 성분",
}

N_FEATURES = len(FEATURE_NAMES)
MIN_FRAMES_PER_WINDOW = 3


def breath_band_ratio(rel_ctx: np.ndarray, t_ctx: np.ndarray) -> float:
    """최근 구간(약 16초)의 1주성분 시계열에서 호흡 대역 전력 비율.

    타임스탬프 간격이 일정하지 않으므로 균일 격자로 보간한 뒤 Welch 스펙트럼을 쓴다.
    10초보다 짧거나 샘플링이 2Hz보다 느리면 계산하지 않는다(NaN).
    """
    if rel_ctx.shape[0] < 20 or t_ctx.size < 20:
        return math.nan
    duration = float(t_ctx[-1] - t_ctx[0])
    if duration < 10.0:
        return math.nan
    fs = min((t_ctx.size - 1) / duration, 20.0)
    if fs < 2.0:
        return math.nan
    x = rel_ctx - rel_ctx.mean(axis=0, keepdims=True)
    try:
        u, s, _ = np.linalg.svd(x, full_matrices=False)
    except np.linalg.LinAlgError:
        return math.nan
    pc = u[:, 0] * s[0]
    grid = np.arange(t_ctx[0], t_ctx[-1], 1.0 / fs)
    if grid.size < 16:
        return math.nan
    y = detrend(np.interp(grid, t_ctx, pc))
    nper = int(min(grid.size, max(16, fs * 8)))
    f, p = welch(y, fs=fs, nperseg=nper)
    total_band = (f >= 0.05) & (f <= min(3.0, fs / 2))
    breath_band = (f >= 0.15) & (f <= 0.6)
    total = float(p[total_band].sum())
    if total <= 0:
        return math.nan
    return float(p[breath_band].sum() / total)


def window_features(
    amp: np.ndarray,
    rssi: np.ndarray,
    mu: np.ndarray,
    mask: np.ndarray,
    rssi_ref: float,
    ctx_amp: np.ndarray | None = None,
    ctx_t: np.ndarray | None = None,
) -> np.ndarray | None:
    """amp: (T, S) 창 안의 진폭, mu/mask: 기준선 평균 진폭과 사용할 서브캐리어.

    프레임이 너무 적으면 None. 반환은 FEATURE_NAMES 순서의 길이 8 배열.
    """
    if amp.ndim != 2 or amp.shape[0] < MIN_FRAMES_PER_WINDOW or amp.shape[1] != mu.shape[0]:
        return None
    m = np.maximum(mu[mask], 1e-6)
    a = amp[:, mask]
    if a.shape[1] < 4:
        return None
    rel = a / m
    sd = rel.std(axis=0)
    motion_med = float(np.median(sd))
    motion_p90 = float(np.percentile(sd, 90))
    diff_mean = float(np.mean(np.abs(np.diff(rel, axis=0))))

    x = rel - rel.mean(axis=0, keepdims=True)
    s = np.linalg.svd(x, compute_uv=False)
    energy = float(np.sum(s**2))
    pc1_ratio = float(s[0] ** 2 / energy) if energy > 1e-12 else 0.0

    prof = a.mean(axis=0)
    if prof.std() > 1e-9 and m.std() > 1e-9:
        static_shift = float(1.0 - np.corrcoef(prof, m)[0, 1])
    else:
        static_shift = 0.0
    level_shift = float(abs(prof.mean() / m.mean() - 1.0))

    rssi_valid = rssi[np.isfinite(rssi)] if rssi.size else rssi
    if rssi_valid.size and math.isfinite(rssi_ref):
        rssi_shift = float(abs(rssi_valid.mean() - rssi_ref))
    else:
        rssi_shift = 0.0

    breath = math.nan
    if ctx_amp is not None and ctx_t is not None and ctx_amp.shape[0] >= 20:
        breath = breath_band_ratio(ctx_amp[:, mask] / m, ctx_t)

    return np.array(
        [motion_med, motion_p90, diff_mean, pc1_ratio, static_shift, level_shift, rssi_shift, breath],
        dtype=np.float64,
    )


def iter_windows(t: np.ndarray, window_s: float):
    """정렬된 시각 배열을 window_s 간격 창으로 나눈다. (i0, i1, t_end)를 낸다."""
    if t.size == 0:
        return
    t_end = float(t[0]) + window_s
    last = float(t[-1])
    i0 = 0
    while t_end <= last + 1e-9:
        i1 = int(np.searchsorted(t, t_end, side="left"))
        i0 = int(np.searchsorted(t, t_end - window_s, side="left"))
        yield i0, i1, t_end
        t_end += window_s


def context_slice(t: np.ndarray, t_end: float, context_s: float) -> tuple[int, int]:
    c0 = int(np.searchsorted(t, t_end - context_s, side="left"))
    c1 = int(np.searchsorted(t, t_end, side="left"))
    return c0, c1


def evidence_from_z(z: dict[str, float], limit: int = 2, min_z: float = 2.0) -> list[str]:
    """z-점수가 큰 특징을 사람이 읽을 근거 문장으로."""
    items = sorted(((k, v) for k, v in z.items() if v is not None and v >= min_z), key=lambda kv: -kv[1])
    out = []
    for k, v in items[:limit]:
        out.append(f"{FEATURE_LABELS_KO.get(k, k)} +{v:.1f}σ")
    return out
