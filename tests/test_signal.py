"""신호처리 계산의 방향성 테스트(합성 신호, 테스트 전용)."""

import numpy as np
import pytest

from app.ml.baseline import Baseline, BaselineError, fit_baseline
from app.ml.csi import Frame, frames_to_arrays, iq_to_amplitude
from app.ml.features import FEATURE_NAMES, context_slice, evidence_from_z, iter_windows
from synthetic import SyntheticRoom


def _arrays(frames):
    return frames_to_arrays([Frame(t, r, iq_to_amplitude(raw)) for t, r, raw in frames])


@pytest.fixture(scope="module")
def room_and_base():
    room = SyntheticRoom(seed=5)
    t, rssi, amp = _arrays(room.frames(40, "empty", seed=1))
    return room, fit_baseline(t, rssi, amp)


def _p_changes(room, base, condition, seed):
    t, rssi, amp = _arrays(room.frames(24, condition, seed=seed, t0=1_700_002_000.0))
    out = []
    for i0, i1, te in iter_windows(t, 2.0):
        c0, c1 = context_slice(t, te, 16.0)
        f = base.features(amp[i0:i1], rssi[i0:i1], amp[c0:c1], t[c0:c1])
        out.append(base.p_change(base.change_score(base.zscore(f))))
    return np.array(out)


def test_baseline_masks_null_subcarriers(room_and_base):
    _, base = room_and_base
    assert base.n_subcarriers == 64
    assert base.mask.sum() == 52  # 0번과 27~37번은 null
    assert base.tau >= 2.0


def test_empty_room_stays_low_and_person_goes_high(room_and_base):
    room, base = room_and_base
    assert _p_changes(room, base, "empty", 21).mean() < 0.3
    assert _p_changes(room, base, "lying", 22).mean() > 0.8
    assert _p_changes(room, base, "moving", 23).mean() > 0.8


def test_baseline_roundtrip(room_and_base):
    _, base = room_and_base
    again = Baseline.from_json(base.to_json())
    assert np.allclose(again.mu, base.mu)
    assert again.tau == base.tau
    assert np.array_equal(again.mask, base.mask)


def test_too_short_calibration_is_rejected():
    room = SyntheticRoom(seed=6)
    t, rssi, amp = _arrays(room.frames(6, "empty"))
    with pytest.raises(BaselineError):
        fit_baseline(t, rssi, amp)


def test_zscore_handles_nan_and_clips(room_and_base):
    _, base = room_and_base
    f = np.full(len(FEATURE_NAMES), np.nan)
    assert np.all(base.zscore(f) == 0)
    f = np.full(len(FEATURE_NAMES), 1e9)
    assert np.all(base.zscore(f) <= 25)


def test_evidence_text():
    z = dict(zip(FEATURE_NAMES, [0.5, 1.0, 0.1, 0.2, 7.5, 3.0, 0.0, 2.5]))
    ev = evidence_from_z(z)
    assert ev[0].startswith("정적 반사 변화") and len(ev) == 2
