"""프레임이 모자란 창: 무선 손실로 프레임이 뭉텅이로 빠지면 특징이 사람 없이도 커질 수 있으므로 학습·판단에서 뺀다."""

from pathlib import Path

import numpy as np

from app.config import settings
from app.ml.baseline import fit_baseline
from app.ml.compare import compare_sessions, format_report
from app.ml.features import MIN_WINDOW_QUALITY, window_quality
from app.ml.sessions import Session
from app.ml.train import _windows


def _room(seconds: float, hz: float, seed: int, t0: float = 0.0):
    rng = np.random.default_rng(seed)
    t = t0 + np.arange(0, seconds, 1 / hz)
    amp = 10 + rng.normal(0, 0.3, (t.size, 52))
    return t, np.full(t.size, -50.0), amp


def _session(label: str, t, rssi, amp, sid: str) -> Session:
    return Session(sid, Path(f"{sid}.csv"), "RX-301", label, "2026-11-04", {"scene": sid}, t, rssi, amp)


def test_window_quality():
    assert window_quality(180, 90.0, 2.0) == 1.0
    assert window_quality(90, 90.0, 2.0) == 0.5
    assert window_quality(400, 90.0, 2.0) == 1.0  # 기준선보다 많아도 1


def test_short_windows_are_left_out_and_counted():
    base = fit_baseline(*_room(60, 90, 0), settings.window_seconds, settings.context_seconds)
    full = _session("empty", *_room(30, 90, 1, t0=100), sid="full")
    ws, cnt = _windows(full, base)
    assert ws is not None and cnt.short == 0 and cnt.total == len(ws.y)

    t, rssi, amp = _room(30, 90, 2, t0=200)
    slow = _session("empty", t[::3], rssi[::3], amp[::3], sid="slow")  # 초당 30개: 기준선의 1/3
    ws, cnt = _windows(slow, base)
    assert ws is None and cnt.total > 0 and cnt.short == cnt.total
    assert cnt.quality_mean < MIN_WINDOW_QUALITY

    # 앞 15초는 정상, 뒤 15초는 절반만 들어온 세션: 뒤쪽 창만 빠진다
    t, rssi, amp = _room(30, 90, 3, t0=300)
    keep = (t < 315) | (np.arange(t.size) % 2 == 0)
    mixed = _session("walking", t[keep], rssi[keep], amp[keep], sid="mixed")
    ws, cnt = _windows(mixed, base)
    assert ws is not None and 0 < cnt.short < cnt.total
    assert len(ws.y) == cnt.total - cnt.short


def test_compare_report_shows_reception_rate():
    t, rssi, amp = _room(60, 90, 4)
    empty = _session("empty", t, rssi, amp, sid="start_empty")
    t, rssi, amp = _room(30, 90, 5, t0=100)
    slow = _session("lying", t[::3], rssi[::3], amp[::3], sid="lying_301")
    report = format_report(compare_sessions([empty, slow]))
    assert "수신율" in report
    assert "lying_301" in report and "학습에서 빠집니다" in report
