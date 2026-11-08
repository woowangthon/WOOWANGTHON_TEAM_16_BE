"""Evaluate annotated real entry times against the shared runtime policy.

Times use a common trial origin. Annotations are facts supplied by an observer;
this module never derives entry or posture times from session labels. Decisions
exist from their window end onward, and cannot be credited before that time.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from numbers import Real
from statistics import median
from typing import Iterable

from ..services.detection import (
    DETECTION_SOURCES,
    MIN_WINDOWS_FOR_NOT_DETECTED,
    RECENT_ROWS,
    detection_thresholds,
    smoothed_probability,
)
from .features import MIN_WINDOW_QUALITY

OUTCOMES = ("timely", "late", "missed", "unjudged", "censored", "preexisting_detection")
JUDGED_STATES = frozenset({"detected", "possible", "not_detected"})
# 2026년 PC epoch(double)의 차감 오차는 약 0.24µs이다. 판정 경계에만 1µs를 허용한다.
TIME_TOLERANCE_S = 1e-6


@dataclass(frozen=True)
class EntryEvent:
    event_id: str
    entered_s: float
    exited_s: float
    pose_s: float | None = None
    label: str = ""


@dataclass(frozen=True)
class EmptyInterval:
    start_s: float
    end_s: float


@dataclass(frozen=True)
class SettledInterval:
    start_s: float
    end_s: float
    label: str = ""


@dataclass(frozen=True)
class Trial:
    trial_id: str
    zone_id: str
    start_s: float
    end_s: float
    events: list[EntryEvent] = field(default_factory=list)
    empty_intervals: list[EmptyInterval] = field(default_factory=list)
    settled_intervals: list[SettledInterval] = field(default_factory=list)


@dataclass(frozen=True)
class Window:
    node: str
    t_end: float
    p_change: float
    p_person: float | None = None
    quality: float | None = 1.0


def _number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _identifier(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def validate_trial(trial: Trial) -> Trial:
    """Return a validated copy; identical copies of one event count only once."""
    if not isinstance(trial, Trial):
        raise ValueError("trial must be a Trial")
    _identifier(trial.trial_id, "trial_id")
    _identifier(trial.zone_id, "zone_id")
    start = _number(trial.start_s, "trial.start_s")
    end = _number(trial.end_s, "trial.end_s")
    if end <= start or not math.isfinite(end - start):
        raise ValueError("trial.end_s must be later than trial.start_s")
    if any(not isinstance(value, (list, tuple))
           for value in (trial.events, trial.empty_intervals, trial.settled_intervals)):
        raise ValueError("events, empty_intervals and settled_intervals must be lists")
    by_id = {}
    for event in trial.events:
        if not isinstance(event, EntryEvent):
            raise ValueError("events must contain EntryEvent objects")
        event_id = _identifier(event.event_id, "event_id")
        entered = _number(event.entered_s, f"{event_id}.entered_s")
        exited = _number(event.exited_s, f"{event_id}.exited_s")
        pose = None if event.pose_s is None else _number(event.pose_s, f"{event_id}.pose_s")
        if (entered >= end or exited <= start or exited <= entered
                or not math.isfinite(exited - entered)):
            raise ValueError(f"{event_id}: occupied event must overlap the trial and exit later than entry")
        if pose is not None and not entered <= pose <= exited:
            raise ValueError(f"{event_id}: pose must be between entry and exit")
        if not isinstance(event.label, str):
            raise ValueError(f"{event_id}.label must be a string")
        normalized = EntryEvent(event_id, entered, exited, pose, event.label)
        if event_id in by_id and by_id[event_id] != normalized:
            raise ValueError(f"conflicting annotations for event_id {event_id}")
        by_id[event_id] = normalized
    events = sorted(by_id.values(), key=lambda e: (e.entered_s, e.event_id))
    for left, right in zip(events, events[1:]):
        if left.exited_s > right.entered_s:
            raise ValueError("occupied entry events must not overlap")
    empties = set()
    for interval in trial.empty_intervals:
        if not isinstance(interval, EmptyInterval):
            raise ValueError("empty_intervals must contain EmptyInterval objects")
        a = _number(interval.start_s, "empty.start_s")
        b = _number(interval.end_s, "empty.end_s")
        if not start <= a < b <= end:
            raise ValueError("empty intervals must be non-empty and within the trial")
        empties.add(EmptyInterval(a, b))
    empties = sorted(empties, key=lambda item: (item.start_s, item.end_s))
    for left, right in zip(empties, empties[1:]):
        if left.end_s > right.start_s:
            raise ValueError("empty intervals must not overlap")
    for interval in empties:
        if any(max(interval.start_s, e.entered_s) < min(interval.end_s, e.exited_s) for e in events):
            raise ValueError("empty intervals must not overlap occupied events")
    settled = set()
    for interval in trial.settled_intervals:
        if not isinstance(interval, SettledInterval):
            raise ValueError("settled_intervals must contain SettledInterval objects")
        a = _number(interval.start_s, "settled.start_s")
        b = _number(interval.end_s, "settled.end_s")
        if not start <= a < b <= end:
            raise ValueError("settled intervals must be non-empty and within the trial")
        if not isinstance(interval.label, str):
            raise ValueError("settled.label must be a string")
        settled.add(SettledInterval(a, b, interval.label))
    settled = sorted(settled, key=lambda item: (item.start_s, item.end_s, item.label))
    for left, right in zip(settled, settled[1:]):
        if left.end_s > right.start_s:
            raise ValueError("settled intervals must not overlap")
    for interval in settled:
        if any(max(interval.start_s, e.entered_s) < min(interval.end_s, e.exited_s) for e in events):
            raise ValueError("settled intervals must not overlap occupied events")
        if any(max(interval.start_s, e.start_s) < min(interval.end_s, e.end_s) for e in empties):
            raise ValueError("settled intervals must not overlap empty intervals")
    return Trial(trial.trial_id, trial.zone_id, start, end, events, empties, settled)


def _windows(trial, windows, person_mode, expected_nodes):
    declared = None
    if expected_nodes is not None:
        if not isinstance(expected_nodes, (list, tuple)):
            raise ValueError("expected_nodes must be a list of node names")
        declared = set(_identifier(n, "expected_nodes item") for n in expected_nodes)
    unique = {}
    for row in windows:
        if not isinstance(row, Window):
            raise ValueError("windows must contain Window objects")
        node = _identifier(row.node, "window.node")
        t = _number(row.t_end, "window.t_end")
        if t > trial.end_s:
            raise ValueError("window.t_end must not be after the trial")
        if declared is not None and node not in declared:
            raise ValueError(f"unexpected node {node}")
        change = _number(row.p_change, "p_change")
        person = None if row.p_person is None else _number(row.p_person, "p_person")
        quality = None if row.quality is None else _number(row.quality, "quality")
        if not 0 <= change <= 1 or (person is not None and not 0 <= person <= 1):
            raise ValueError("probabilities must be between zero and one")
        if quality is not None and not 0 <= quality <= 1:
            raise ValueError("quality must be between zero and one")
        if person_mode and person is None:
            raise ValueError("person_mode requires p_person for every window")
        # Runtime stores four decimals for predictions and three for quality.
        normalized = Window(node, t, round(change, 4),
                            round(person, 4) if person_mode else None,
                            None if quality is None else round(quality, 3))
        key = node, t
        if key in unique and unique[key] != normalized:
            raise ValueError(f"conflicting windows for {node} at {t}")
        unique[key] = normalized
    rows = sorted(unique.values(), key=lambda r: (r.t_end, r.node))
    nodes = declared if declared is not None else {r.node for r in rows}
    # A partial observation may start after recording began. Preserve runtime
    # context, but bound prefix history exactly as the live recent-row query.
    prefix = defaultdict(lambda: deque(maxlen=RECENT_ROWS))
    observed = []
    for row in rows:
        if row.t_end < trial.start_s:
            prefix[row.node].append(row)
        else:
            observed.append(row)
    rows = sorted([r for history in prefix.values() for r in history] + observed,
                  key=lambda r: (r.t_end, r.node))
    return rows, sorted(nodes)


def _good(row) -> bool:
    return row.quality is None or row.quality >= MIN_WINDOW_QUALITY


def _zone_snapshot(history, t, detect, possible, stale_s):
    ok = []
    for rows in history.values():
        if not rows or not _good(rows[-1]):
            continue
        good = [r for r in rows if _good(r)]
        if not good or t - good[-1].t_end > stale_s:
            continue
        p = smoothed_probability(rows)
        if p is not None:
            ok.append((p, len(good)))
    if not ok:
        return "no_signal", None, 0
    p = max(item[0] for item in ok)
    state = ("detected" if p >= detect else "possible" if p >= possible else
             "not_detected" if max(item[1] for item in ok) >= MIN_WINDOWS_FOR_NOT_DETECTED else "warming")
    return state, p, len(ok)


def _segments(trial, rows, nodes, detect, possible, stale_s):
    updates = defaultdict(list)
    boundaries = {trial.start_s, trial.end_s}
    for row in rows:
        updates[row.t_end].append(row)
        boundaries.add(row.t_end)
        expiry = row.t_end + stale_s
        if _good(row) and row.t_end < expiry < trial.end_s:
            boundaries.add(expiry)
    boundaries = sorted(boundaries)
    history = {node: deque(maxlen=RECENT_ROWS) for node in nodes}
    result = []
    for start, end in zip(boundaries, boundaries[1:]):
        for row in updates[start]:
            history[row.node].append(row)
        # At the exact expiry instant runtime is still valid; a point has zero
        # duration. The following half-open time segment is already stale.
        state, p, available = _zone_snapshot(history, math.nextafter(start, math.inf),
                                             detect, possible, stale_s)
        segment = {"start_s": start, "end_s": end, "state": state, "p": p,
                   "nodes_available": available}
        if (result and result[-1]["state"] == state and result[-1]["p"] == p
                and result[-1]["nodes_available"] == available):
            result[-1]["end_s"] = end
        else:
            result.append(segment)
    return result


def _state_at(segments, t):
    return next((s["state"] for s in segments if s["start_s"] <= t < s["end_s"]), "no_signal")


def _duration(segments, start, end):
    detected = valid = 0.0
    for segment in segments:
        seconds = max(0.0, min(end, segment["end_s"]) - max(start, segment["start_s"]))
        if segment["state"] in JUDGED_STATES:
            valid += seconds
        if segment["state"] == "detected":
            detected += seconds
    total = max(0.0, end - start)
    return {"total_s": total, "detected_s": detected, "valid_s": valid,
            "coverage": valid / total if total else None}


def _stable_interval(start, end, segments):
    if end <= start:
        return {"status": "unavailable", "reason": "no_stable_observation_interval", "start_s": start,
                "end_s": end, "total_s": 0.0, "detected_s": 0.0, "valid_s": 0.0,
                "coverage": None, "hold_ratio": None, "hold_ratio_known": None}
    out = _duration(segments, start, end)
    return {"status": "evaluated" if out["valid_s"] else "unjudged", "reason": None,
            "start_s": start, "end_s": end, **out,
            "hold_ratio": out["detected_s"] / out["total_s"],
            "hold_ratio_known": out["detected_s"] / out["valid_s"] if out["valid_s"] else None}


def _stable(event, trial, segments, stable_after_s, exit_margin_s):
    if event.pose_s is None:
        return {"status": "unavailable", "reason": "missing_pose_time", "start_s": None,
                "end_s": None, "total_s": 0.0, "detected_s": 0.0, "valid_s": 0.0,
                "coverage": None, "hold_ratio": None, "hold_ratio_known": None}
    start = max(trial.start_s, event.entered_s + stable_after_s, event.pose_s)
    end = min(event.exited_s - exit_margin_s, trial.end_s)
    return _stable_interval(start, end, segments)


def _entry(event, trial, segments, timeout_s, stable_after_s, exit_margin_s):
    end = min(event.exited_s, trial.end_s)
    start = max(event.entered_s, trial.start_s)
    entry_observed = event.entered_s >= trial.start_s
    observation = _duration(segments, start, end)
    preexisting = (_state_at(segments, math.nextafter(event.entered_s, -math.inf)) == "detected"
                   if entry_observed else None)
    armed = not preexisting
    hit = None
    for segment in segments:
        if not entry_observed:
            break  # Prefix history cannot establish an unobserved entry response.
        if segment["end_s"] <= start or segment["start_s"] >= end:
            continue
        state = segment["state"]
        if state in {"possible", "not_detected"}:
            armed = True
        if state == "detected" and armed:
            hit = max(segment["start_s"], event.entered_s)
            break
    latency = None if hit is None else hit - event.entered_s
    reason = None
    if not entry_observed:
        outcome = "censored"
        reason = "entry_before_observation"
    elif latency is not None:
        within_limit = latency <= timeout_s or math.isclose(latency, timeout_s, rel_tol=0, abs_tol=TIME_TOLERANCE_S)
        outcome = "timely" if within_limit else "late"
    elif not observation["valid_s"]:
        outcome = "unjudged"
    elif preexisting:
        outcome = "preexisting_detection"
    elif (end - event.entered_s < timeout_s
          and not math.isclose(end - event.entered_s, timeout_s, rel_tol=0, abs_tol=TIME_TOLERANCE_S)):
        outcome = "censored"
    else:
        outcome = "missed"
    return {"event_id": event.event_id, "label": event.label, "entered_s": event.entered_s,
            "exited_s": event.exited_s, "pose_s": event.pose_s, "observation_end_s": end,
            "observation_start_s": start, "entry_observed": entry_observed,
            "outcome": outcome, "reason": reason,
            "preexisting_at_entry": preexisting, "detected_at_s": hit,
            "latency_s": latency, "observed_s": observation["total_s"],
            "detected_s": observation["detected_s"], "valid_s": observation["valid_s"],
            "coverage": observation["coverage"],
            "stable": _stable(event, trial, segments, stable_after_s, exit_margin_s)}


def _empty(trial, segments):
    # Unknown gaps neither assert that an alarm stopped nor create new episodes.
    episode_for = {}
    episode_starts = {}
    active = None
    for i, segment in enumerate(segments):
        if segment["state"] in {"possible", "not_detected"}:
            active = None
        elif segment["state"] == "detected":
            if active is None:
                active = len(episode_starts)
                episode_starts[active] = segment["start_s"]
            episode_for[i] = active
    episodes, carry_in = set(), set()
    total = detected = valid = 0.0
    for interval in trial.empty_intervals:
        stats = _duration(segments, interval.start_s, interval.end_s)
        total += stats["total_s"]
        detected += stats["detected_s"]
        valid += stats["valid_s"]
        for i, segment in enumerate(segments):
            if (i not in episode_for or max(interval.start_s, segment["start_s"])
                    >= min(interval.end_s, segment["end_s"])):
                continue
            episode = episode_for[i]
            episodes.add(episode)
            if (episode_starts[episode] < interval.start_s
                    and _state_at(segments, interval.start_s) == "detected"):
                carry_in.add(episode)
    return {"episodes": len(episodes), "carry_in_episodes": len(carry_in), "carry_in_high": bool(carry_in),
            "total_s": total, "detected_s": detected, "valid_s": valid,
            "coverage": valid / total if total else None,
            "false_positive_ratio_known": detected / valid if valid else None,
            "episodes_per_valid_hour": len(episodes) * 3600 / valid if valid else None}


def _window_counts(trial, rows, nodes, window_s):
    history_count = sum(row.t_end < trial.start_s for row in rows)
    rows = [row for row in rows if row.t_end >= trial.start_s]
    expected = math.floor((trial.end_s - trial.start_s) / window_s)
    seen = set()
    for row in rows:
        # Slots describe expected complete windows; a sample at the recording
        # start or in its final partial window does not complete a new slot.
        slot = math.ceil((row.t_end - trial.start_s) / window_s - 1e-9) - 1
        if 0 <= slot < expected:
            seen.add((row.node, slot))
    observed_zone = len({slot for _, slot in seen})
    expected_node = expected * len(nodes)
    bad = sum(not _good(row) for row in rows)
    return {"nodes": len(nodes), "history_node_windows": history_count, "expected_zone_slots": expected, "observed_zone_slots": observed_zone,
            "missing_zone_slots": expected - observed_zone, "expected_node_slots": expected_node,
            "observed_node_slots": len(seen), "missing_node_slots": expected_node - len(seen),
            "node_slot_coverage": len(seen) / expected_node if expected_node else None,
            "observed_node_windows": len(rows), "bad_quality_node_windows": bad,
            "quality_coverage": (len(rows) - bad) / len(rows) if rows else None}


def evaluate_trial(trial: Trial, windows: Iterable[Window], *, source: str = "measurement",
                   person_mode: bool = False, timeout_s: float = 30, stable_after_s: float = 0,
                   exit_margin_s: float = 0, window_s: float = 2, stale_s: float = 6,
                   expected_nodes: list[str] | None = None) -> dict:
    """Measure one annotated zone trial without inferring any ground-truth time."""
    trial = validate_trial(trial)
    if source not in DETECTION_SOURCES:
        raise ValueError("source must be alarm, manual, measurement, or replay")
    if not isinstance(person_mode, bool):
        raise ValueError("person_mode must be a bool")
    options = {name: _number(value, name) for name, value in
               (("timeout_s", timeout_s), ("stable_after_s", stable_after_s),
                ("exit_margin_s", exit_margin_s), ("window_s", window_s), ("stale_s", stale_s))}
    if any(options[n] <= 0 for n in ("timeout_s", "window_s", "stale_s")):
        raise ValueError("timeout_s, window_s, and stale_s must be positive")
    if options["stable_after_s"] < 0 or options["exit_margin_s"] < 0:
        raise ValueError("stable_after_s and exit_margin_s must be non-negative")
    rows, nodes = _windows(trial, windows, person_mode, expected_nodes)
    detect, possible = detection_thresholds(source, person_mode)
    segments = _segments(trial, rows, nodes, detect, possible, options["stale_s"])
    events = [_entry(e, trial, segments, options["timeout_s"], options["stable_after_s"],
                     options["exit_margin_s"]) for e in trial.events]
    settled = [{**_stable_interval(interval.start_s, interval.end_s, segments), "label": interval.label}
               for interval in trial.settled_intervals]
    counts = {"events": len(events), **{name: sum(e["outcome"] == name for e in events) for name in OUTCOMES}}
    has_annotations = bool(events or trial.empty_intervals or settled)
    return {"status": "evaluated" if has_annotations else "unavailable",
            "reason": None if has_annotations else "no_actual_event_annotations",
            "trial_id": trial.trial_id, "zone_id": trial.zone_id, "source": source,
            "person_mode": person_mode, "thresholds": {"detected": detect, "possible": possible},
            "options": options, "counts": counts, "events": events, "settled_intervals": settled,
            "empty": _empty(trial, segments), "windows": _window_counts(trial, rows, nodes, options["window_s"]),
            "segments": [{**segment, "start_s": max(segment["start_s"], trial.start_s)}
                         for segment in segments if segment["end_s"] > trial.start_s]}


def summarize_trials(reports: Iterable[dict]) -> dict:
    """Aggregate each trial/zone once and label success-only latency explicitly."""
    unique = {}
    for report in reports:
        key = report["trial_id"], report["zone_id"]
        if key in unique and unique[key] != report:
            raise ValueError(f"conflicting reports for trial/zone {key}")
        unique[key] = report
    reports = list(unique.values())
    counts = {name: sum(r["counts"][name] for r in reports) for name in ("events", *OUTCOMES)}
    counts["trials"] = len(reports)
    latencies = [e["latency_s"] for r in reports for e in r["events"]
                 if e["outcome"] in {"timely", "late"}]
    before_observation = sum(e.get("reason") == "entry_before_observation"
                             for r in reports for e in r["events"])
    n_entry_evaluable = counts["events"] - before_observation
    stable_events = [e["stable"] for r in reports for e in r["events"] if e["stable"]["total_s"] > 0]
    stable_intervals = [s for r in reports for s in r.get("settled_intervals", []) if s["total_s"] > 0]
    stable_rows = stable_events + stable_intervals
    stable = {name: sum(s[name] for s in stable_rows) for name in ("total_s", "detected_s", "valid_s")}
    stable.update(status="available" if stable["valid_s"] else
                  "unjudged" if stable["total_s"] else "unavailable",
                  events_available=len(stable_events), settled_intervals_available=len(stable_intervals),
                  hold_ratio=stable["detected_s"] / stable["total_s"] if stable["total_s"] else None,
                  hold_ratio_known=stable["detected_s"] / stable["valid_s"] if stable["valid_s"] else None,
                  coverage=stable["valid_s"] / stable["total_s"] if stable["total_s"] else None)
    empty = {name: sum(r["empty"][name] for r in reports)
             for name in ("episodes", "carry_in_episodes", "total_s", "detected_s", "valid_s")}
    empty.update(carry_in_high=bool(empty["carry_in_episodes"]),
                 coverage=empty["valid_s"] / empty["total_s"] if empty["total_s"] else None,
                 false_positive_ratio_known=empty["detected_s"] / empty["valid_s"] if empty["valid_s"] else None,
                 episodes_per_valid_hour=empty["episodes"] * 3600 / empty["valid_s"] if empty["valid_s"] else None)
    window_keys = ("history_node_windows", "expected_zone_slots", "observed_zone_slots", "missing_zone_slots", "expected_node_slots",
                   "observed_node_slots", "missing_node_slots", "observed_node_windows", "bad_quality_node_windows")
    windows = {name: sum(r["windows"][name] for r in reports) for name in window_keys}
    windows["node_slot_coverage"] = (windows["observed_node_slots"] / windows["expected_node_slots"]
                                     if windows["expected_node_slots"] else None)
    windows["quality_coverage"] = (1 - windows["bad_quality_node_windows"] / windows["observed_node_windows"]
                                  if windows["observed_node_windows"] else None)
    has_annotations = bool(counts["events"] or empty["total_s"] or stable["total_s"])
    return {"status": "evaluated" if has_annotations else "unavailable",
            "reason": None if has_annotations else "no_actual_event_annotations",
            "counts": counts, "n_detected": len(latencies),
            "entry_response_status": ("unavailable" if not n_entry_evaluable else
                                      "partial" if before_observation else "available"),
            "n_entry_evaluable": n_entry_evaluable, "n_entry_before_observation": before_observation,
            "median_seconds_to_detect_successes": median(latencies) if latencies else None,
            "timely_rate_all_events": (counts["timely"] / counts["events"]
                                       if counts["events"] and not before_observation else None),
            "detected_rate_all_events": (len(latencies) / counts["events"]
                                         if counts["events"] and not before_observation else None),
            "timely_rate_evaluable_events": counts["timely"] / n_entry_evaluable if n_entry_evaluable else None,
            "detected_rate_evaluable_events": len(latencies) / n_entry_evaluable if n_entry_evaluable else None,
            "stable": stable, "empty": empty, "windows": windows}
