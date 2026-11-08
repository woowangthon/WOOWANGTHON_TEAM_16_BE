"""Read explicit observed-entry annotations without inferring entry from scene labels."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from numbers import Real

from .event_evaluation import EmptyInterval, EntryEvent, SettledInterval, Trial, validate_trial
from .sessions import PERSON_LABELS, Session


class AnnotationError(ValueError):
    """The annotation file is malformed or cannot identify its recorded sessions."""


@dataclass(frozen=True)
class AnnotatedTrial:
    trial: Trial
    session_ids: list[str]
    origin_epoch_s: float


def _object(value, where: str, required: set[str], optional: set[str] | None = None) -> dict:
    if not isinstance(value, dict):
        raise AnnotationError(f"{where}: expected an object")
    missing = required - value.keys()
    if missing:
        raise AnnotationError(f"{where}: missing keys: {', '.join(sorted(missing))}")
    unknown = value.keys() - required - (optional or set())
    if unknown:
        raise AnnotationError(f"{where}: unknown keys: {', '.join(sorted(unknown))}")
    return value


def _list(value, where: str, *, nonempty: bool = False) -> list:
    if not isinstance(value, list) or (nonempty and not value):
        raise AnnotationError(f"{where}: expected {'a nonempty' if nonempty else 'a'} list")
    return value


def _text(value, where: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise AnnotationError(f"{where}: expected {'a' if allow_empty else 'a nonempty'} string")
    return value


def _number(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise AnnotationError(f"{where}: expected a finite number, not a boolean or string")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise AnnotationError(f"{where}: expected a finite number") from exc
    if not math.isfinite(number):
        raise AnnotationError(f"{where}: expected a finite number")
    return number


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise AnnotationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_event_annotations(path: Path, sessions: list[Session]) -> list[AnnotatedTrial]:
    """Load trials, resolving relative seconds against an explicitly recorded origin.

    ``origin_session_id`` uses only that session's ``meta.started_at`` epoch.
    The first CSV frame is deliberately never used as a fallback. ``origin_epoch_s``
    is available for recordings that have an independently established time origin.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AnnotationError(f"{path}: could not read annotation JSON: {exc}") from exc
    except AnnotationError as exc:
        raise AnnotationError(f"{path}: {exc}") from exc
    try:
        return _load_trials(raw, sessions)
    except ValueError as exc:
        raise AnnotationError(f"{path}: {exc}") from exc


def _load_trials(raw, sessions: list[Session]) -> list[AnnotatedTrial]:
    top = _object(raw, "annotations", {"schema_version", "trials"})
    if type(top["schema_version"]) is not int or top["schema_version"] != 1:
        raise AnnotationError("annotations.schema_version: expected integer 1")
    entries = _list(top["trials"], "annotations.trials", nonempty=True)
    by_id: dict[str, Session] = {}
    for session in sessions:
        if session.session_id in by_id:
            raise AnnotationError(f"recorded sessions: duplicate session_id {session.session_id!r}")
        by_id[session.session_id] = session

    trials = []
    trial_keys = set()
    used_sessions: set[str] = set()
    for index, value in enumerate(entries):
        where = f"trials[{index}]"
        obj = _object(value, where,
                      {"trial_id", "zone_id", "session_ids", "start_s", "end_s", "events", "empty_intervals"},
                      {"origin_session_id", "origin_epoch_s", "settled_intervals"})
        trial_id = _text(obj["trial_id"], f"{where}.trial_id")
        zone_id = _text(obj["zone_id"], f"{where}.zone_id")
        key = (trial_id, zone_id)
        if key in trial_keys:
            raise AnnotationError(f"{where}: duplicate (trial_id, zone_id): {key!r}")
        trial_keys.add(key)

        ids = [_text(sid, f"{where}.session_ids[{i}]")
               for i, sid in enumerate(_list(obj["session_ids"], f"{where}.session_ids", nonempty=True))]
        if len(set(ids)) != len(ids):
            raise AnnotationError(f"{where}: duplicate session_id within trial")
        unknown = set(ids) - by_id.keys()
        if unknown:
            raise AnnotationError(f"{where}: unknown session_id: {', '.join(sorted(unknown))}")
        reused = used_sessions.intersection(ids)
        if reused:
            raise AnnotationError(f"{where}: session_id reused by another trial: {', '.join(sorted(reused))}")
        nodes = [by_id[sid].node for sid in ids]
        if len(set(nodes)) != len(nodes):
            raise AnnotationError(f"{where}: multiple sessions from the same node are not allowed")
        used_sessions.update(ids)

        origin_keys = {"origin_session_id", "origin_epoch_s"}.intersection(obj)
        if len(origin_keys) != 1:
            raise AnnotationError(f"{where}: specify exactly one of origin_session_id or origin_epoch_s")
        if "origin_session_id" in obj:
            sid = _text(obj["origin_session_id"], f"{where}.origin_session_id")
            if sid not in ids:
                raise AnnotationError(f"{where}: origin_session_id must be in session_ids")
            if "started_at" not in by_id[sid].meta:
                raise AnnotationError(f"{where}: {sid!r} has no recorded started_at; specify origin_epoch_s explicitly")
            origin = _number(by_id[sid].meta["started_at"], f"{where}.origin_session_id.started_at")
        else:
            origin = _number(obj["origin_epoch_s"], f"{where}.origin_epoch_s")

        events = []
        for i, value in enumerate(_list(obj["events"], f"{where}.events")):
            loc = f"{where}.events[{i}]"
            event = _object(value, loc, {"event_id", "entered_s", "exited_s"}, {"pose_s", "label"})
            pose = event.get("pose_s")
            events.append(EntryEvent(
                event_id=_text(event["event_id"], f"{loc}.event_id"),
                entered_s=_number(event["entered_s"], f"{loc}.entered_s"),
                exited_s=_number(event["exited_s"], f"{loc}.exited_s"),
                pose_s=None if pose is None else _number(pose, f"{loc}.pose_s"),
                label=_text(event.get("label", ""), f"{loc}.label", allow_empty=True),
            ))
        empty_intervals = []
        for i, value in enumerate(_list(obj["empty_intervals"], f"{where}.empty_intervals")):
            loc = f"{where}.empty_intervals[{i}]"
            interval = _object(value, loc, {"start_s", "end_s"})
            empty_intervals.append(EmptyInterval(
                start_s=_number(interval["start_s"], f"{loc}.start_s"),
                end_s=_number(interval["end_s"], f"{loc}.end_s"),
            ))
        settled_intervals = []
        for i, value in enumerate(_list(obj.get("settled_intervals", []), f"{where}.settled_intervals")):
            loc = f"{where}.settled_intervals[{i}]"
            interval = _object(value, loc, {"start_s", "end_s"}, {"label"})
            settled_intervals.append(SettledInterval(
                start_s=_number(interval["start_s"], f"{loc}.start_s"),
                end_s=_number(interval["end_s"], f"{loc}.end_s"),
                label=_text(interval.get("label", ""), f"{loc}.label", allow_empty=True),
            ))
        trial = Trial(trial_id=trial_id, zone_id=zone_id,
                      start_s=_number(obj["start_s"], f"{where}.start_s"),
                      end_s=_number(obj["end_s"], f"{where}.end_s"),
                      events=events, empty_intervals=empty_intervals, settled_intervals=settled_intervals)
        trial = validate_trial(trial)
        trials.append(AnnotatedTrial(trial=trial, session_ids=ids, origin_epoch_s=origin))
    return trials


def labels_for_windows(annotation: AnnotatedTrial, starts_epoch: Sequence[float],
                       ends_epoch: Sequence[float]) -> list[str | None]:
    """Label only windows fully covered by observed emptiness or a known settled pose.

    Entry-to-pose time, unknown poses, boundary crossings and unobserved time are
    excluded from training. Explicit settled intervals can label recorded steady
    time without inventing an entry or borrowing the scene's label. This mask
    does not remove time from entry evaluation.
    """
    if not isinstance(annotation, AnnotatedTrial):
        raise AnnotationError("annotation must be an AnnotatedTrial")
    if isinstance(starts_epoch, (str, bytes, dict)) or isinstance(ends_epoch, (str, bytes, dict)):
        raise AnnotationError("window starts and ends must be sequences of epoch seconds")
    try:
        starts, ends = list(starts_epoch), list(ends_epoch)
    except TypeError as exc:
        raise AnnotationError("window starts and ends must be sequences of epoch seconds") from exc
    if len(starts) != len(ends):
        raise AnnotationError("window starts and ends must have the same length")
    trial = validate_trial(annotation.trial)
    origin = _number(annotation.origin_epoch_s, "origin_epoch_s")

    def absolute(offset: float, where: str) -> float:
        return _number(origin + offset, where)

    observed_start = absolute(trial.start_s, "trial start epoch")
    observed_end = absolute(trial.end_s, "trial end epoch")
    empty_ranges = [(absolute(interval.start_s, "empty start epoch"),
                     absolute(interval.end_s, "empty end epoch")) for interval in trial.empty_intervals]
    pose_ranges = [(absolute(event.pose_s, "pose epoch"), absolute(event.exited_s, "exit epoch"), event.label)
                   for event in trial.events if event.pose_s is not None and event.label in PERSON_LABELS]
    pose_ranges.extend(
        (absolute(interval.start_s, "settled start epoch"), absolute(interval.end_s, "settled end epoch"), interval.label)
        for interval in trial.settled_intervals if interval.label in PERSON_LABELS
    )
    labels = []
    for index, (start, end) in enumerate(zip(starts, ends)):
        start = _number(start, f"windows[{index}].start_epoch")
        end = _number(end, f"windows[{index}].end_epoch")
        if end <= start:
            raise AnnotationError(f"windows[{index}]: end epoch must be later than start epoch")
        label = None
        if observed_start <= start and end <= observed_end:
            if any(a <= start and end <= b for a, b in empty_ranges):
                label = "empty"
            else:
                label = next((label for a, b, label in pose_ranges if a <= start and end <= b), None)
        labels.append(label)
    return labels
