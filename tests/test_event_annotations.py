"""Observed entry times must have an explicit clock and unambiguous session references."""

from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pytest

from app.ml.event_annotations import (
    AnnotatedTrial, AnnotationError, labels_for_windows, load_event_annotations,
)
from app.ml.sessions import Session

EPOCH = 1_790_000_000.0


def recorded_session(sid="scene-rx-a", node="RX-A", *, started_at=EPOCH):
    meta = {} if started_at is None else {"started_at": started_at}
    return Session(sid, Path(f"{sid}.csv"), node, "lying", "2026-11-07", meta,
                   t=np.array([EPOCH + 0.25, EPOCH + 119.9]))


def valid_document():
    return {
        "schema_version": 1,
        "trials": [{
            "trial_id": "observed-entry-1", "zone_id": "224",
            "session_ids": ["scene-rx-a"], "origin_session_id": "scene-rx-a",
            "start_s": 0, "end_s": 120,
            "events": [{"event_id": "entry-1", "entered_s": 12, "pose_s": 18,
                        "exited_s": 90, "label": "lying"}],
            "empty_intervals": [{"start_s": 0, "end_s": 12}, {"start_s": 90, "end_s": 120}],
        }],
    }


def write_document(tmp_path, document):
    path = tmp_path / "observed-events.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_recorded_origin_uses_started_at_not_first_csv_frame(tmp_path):
    sessions = [recorded_session()]
    result = load_event_annotations(write_document(tmp_path, valid_document()), sessions)
    assert len(result) == 1
    observed = result[0]
    assert observed.origin_epoch_s == EPOCH
    assert observed.origin_epoch_s != sessions[0].start
    assert observed.session_ids == ["scene-rx-a"]
    assert observed.trial.events[0].entered_s == 12
    assert observed.trial.events[0].pose_s == 18
    assert observed.trial.events[0].exited_s == 90
    assert observed.origin_epoch_s + observed.trial.events[0].entered_s == EPOCH + 12


def test_explicit_origin_allows_legacy_session_without_started_at(tmp_path):
    doc = valid_document()
    trial = doc["trials"][0]
    del trial["origin_session_id"]
    trial["origin_epoch_s"] = EPOCH - 0.5
    result = load_event_annotations(write_document(tmp_path, doc), [recorded_session(started_at=None)])
    assert result[0].origin_epoch_s == EPOCH - 0.5


def test_multiple_receivers_in_one_zone_count_one_observed_entry(tmp_path):
    doc = valid_document()
    doc["trials"][0]["session_ids"].append("scene-rx-b")
    sessions = [recorded_session(), recorded_session("scene-rx-b", "RX-B")]
    result = load_event_annotations(write_document(tmp_path, doc), sessions)
    assert result[0].session_ids == ["scene-rx-a", "scene-rx-b"]
    assert len(result[0].trial.events) == 1


def test_missing_recorded_origin_never_falls_back_to_csv_time(tmp_path):
    with pytest.raises(AnnotationError, match="no recorded started_at"):
        load_event_annotations(write_document(tmp_path, valid_document()), [recorded_session(started_at=None)])


@pytest.mark.parametrize("origin", [True, "1790000000", float("nan"), float("inf")])
def test_recorded_origin_must_be_a_finite_number(tmp_path, origin):
    with pytest.raises(AnnotationError, match="finite number"):
        load_event_annotations(write_document(tmp_path, valid_document()), [recorded_session(started_at=origin)])


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_schema_version_is_strict_integer_one(tmp_path, version):
    doc = valid_document()
    doc["schema_version"] = version
    with pytest.raises(AnnotationError, match="schema_version"):
        load_event_annotations(write_document(tmp_path, doc), [recorded_session()])


@pytest.mark.parametrize("field,value", [
    ("start_s", True), ("end_s", "120"), ("end_s", float("inf")),
])
def test_trial_times_are_strict_finite_numbers(tmp_path, field, value):
    doc = valid_document()
    doc["trials"][0][field] = value
    with pytest.raises(AnnotationError, match="finite number"):
        load_event_annotations(write_document(tmp_path, doc), [recorded_session()])


@pytest.mark.parametrize("field,value", [
    ("entered_s", False), ("entered_s", float("nan")), ("pose_s", "18"),
    ("pose_s", float("inf")), ("exited_s", True),
])
def test_observed_times_are_strict_finite_numbers(tmp_path, field, value):
    doc = valid_document()
    doc["trials"][0]["events"][0][field] = value
    with pytest.raises(AnnotationError, match="finite number"):
        load_event_annotations(write_document(tmp_path, doc), [recorded_session()])


@pytest.mark.parametrize("location", ["top", "trial", "event", "empty"])
def test_unknown_keys_are_rejected_instead_of_silently_ignored(tmp_path, location):
    doc = valid_document()
    target = {"top": doc, "trial": doc["trials"][0], "event": doc["trials"][0]["events"][0],
              "empty": doc["trials"][0]["empty_intervals"][0]}[location]
    target["misspelled_field"] = 1
    with pytest.raises(AnnotationError, match="unknown keys"):
        load_event_annotations(write_document(tmp_path, doc), [recorded_session()])


@pytest.mark.parametrize("origins", ["both", "neither", "not-in-trial"])
def test_origin_reference_is_explicit_and_belongs_to_the_trial(tmp_path, origins):
    doc = valid_document()
    trial = doc["trials"][0]
    if origins == "both":
        trial["origin_epoch_s"] = EPOCH
    elif origins == "neither":
        del trial["origin_session_id"]
    else:
        trial["origin_session_id"] = "scene-rx-b"
    with pytest.raises(AnnotationError, match="exactly one|must be in session_ids"):
        load_event_annotations(write_document(tmp_path, doc), [recorded_session(), recorded_session("scene-rx-b", "RX-B")])


@pytest.mark.parametrize("problem", ["unknown", "duplicate", "same-node", "reused", "duplicate-trial"])
def test_ambiguous_or_reused_session_references_are_rejected(tmp_path, problem):
    doc = valid_document()
    sessions = [recorded_session()]
    trial = doc["trials"][0]
    if problem == "unknown":
        trial["session_ids"].append("missing")
    elif problem == "duplicate":
        trial["session_ids"].append("scene-rx-a")
    elif problem == "same-node":
        trial["session_ids"].append("scene-rx-a-second")
        sessions.append(recorded_session("scene-rx-a-second", "RX-A"))
    else:
        other = deepcopy(trial)
        if problem == "reused":
            other["trial_id"] = "different-trial"
        doc["trials"].append(other)
    with pytest.raises(AnnotationError, match="unknown session_id|duplicate|same node|reused"):
        load_event_annotations(write_document(tmp_path, doc), sessions)


def test_duplicate_session_ids_in_recorded_corpus_are_rejected(tmp_path):
    with pytest.raises(AnnotationError, match="duplicate session_id"):
        load_event_annotations(write_document(tmp_path, valid_document()),
                               [recorded_session(), recorded_session(node="RX-B")])


@pytest.mark.parametrize("problem", ["reverse-entry", "pose-outside", "event-overlap", "empty-overlap", "outside-trial"])
def test_core_validates_observed_time_order_and_overlaps(tmp_path, problem):
    doc = valid_document()
    trial = doc["trials"][0]
    if problem == "reverse-entry":
        trial["events"][0]["entered_s"] = 91
    elif problem == "pose-outside":
        trial["events"][0]["pose_s"] = 11
    elif problem == "event-overlap":
        trial["events"].append({"event_id": "entry-2", "entered_s": 50, "exited_s": 100})
    elif problem == "empty-overlap":
        trial["empty_intervals"].append({"start_s": 20, "end_s": 30})
    else:
        trial["events"][0].update(entered_s=-10, pose_s=-9, exited_s=-1)
    with pytest.raises(AnnotationError):
        load_event_annotations(write_document(tmp_path, doc), [recorded_session()])


def test_optional_pose_and_label_are_not_invented(tmp_path):
    doc = valid_document()
    del doc["trials"][0]["events"][0]["pose_s"]
    del doc["trials"][0]["events"][0]["label"]
    result = load_event_annotations(write_document(tmp_path, doc), [recorded_session()])
    assert result[0].trial.events[0].pose_s is None
    assert result[0].trial.events[0].label == ""


def test_duplicate_json_keys_and_invalid_json_fail_with_file_context(tmp_path):
    path = tmp_path / "observed-events.json"
    for content in ('{"schema_version":1,"schema_version":1,"trials":[]}', '{"trials":'):
        path.write_text(content, encoding="utf-8")
        with pytest.raises(AnnotationError, match="observed-events.json"):
            load_event_annotations(path, [recorded_session()])


def test_normalized_trial_collapses_identical_event_copies(tmp_path):
    doc = valid_document()
    trial = doc["trials"][0]
    trial["events"].append(deepcopy(trial["events"][0]))
    result = load_event_annotations(write_document(tmp_path, doc), [recorded_session()])
    assert len(result[0].trial.events) == 1
    assert result[0].trial.events[0].event_id == "entry-1"


def test_conflicting_copies_of_one_observed_event_are_rejected(tmp_path):
    doc = valid_document()
    duplicate = deepcopy(doc["trials"][0]["events"][0])
    duplicate["entered_s"] = 13
    doc["trials"][0]["events"].append(duplicate)
    with pytest.raises(AnnotationError):
        load_event_annotations(write_document(tmp_path, doc), [recorded_session()])


def test_scene_person_label_does_not_create_unobserved_entry_events(tmp_path):
    doc = valid_document()
    doc["trials"][0]["events"] = []
    doc["trials"][0]["empty_intervals"] = [{"start_s": 0, "end_s": 120}]
    result = load_event_annotations(write_document(tmp_path, doc), [recorded_session()])
    assert result[0].trial.events == []


def test_zero_second_entry_and_pose_are_valid_observed_times(tmp_path):
    doc = valid_document()
    event = doc["trials"][0]["events"][0]
    event["entered_s"] = event["pose_s"] = 0
    doc["trials"][0]["empty_intervals"] = [{"start_s": 90, "end_s": 120}]
    result = load_event_annotations(write_document(tmp_path, doc), [recorded_session()])
    assert result[0].trial.events[0].entered_s == 0
    assert result[0].trial.events[0].pose_s == 0


def test_window_labels_require_full_observed_interval_containment(tmp_path):
    annotation = load_event_annotations(write_document(tmp_path, valid_document()), [recorded_session()])[0]
    starts = np.array([-2, 0, 10, 11, 12, 16, 18, 88, 89, 90, 118, 120]) + EPOCH
    ends = np.array([0, 2, 12, 13, 14, 18, 20, 90, 91, 92, 120, 122]) + EPOCH
    assert labels_for_windows(annotation, starts, ends) == [
        None, "empty", "empty", None, None, None, "lying", "lying", None, "empty", "empty", None,
    ]


def test_window_labels_use_annotation_origin_not_first_csv_frame(tmp_path):
    annotation = load_event_annotations(write_document(tmp_path, valid_document()), [recorded_session()])[0]
    # The first frame is 0.25 s late; metadata origin makes [18,20] fully settled.
    assert labels_for_windows(annotation, [EPOCH + 18], [EPOCH + 20]) == ["lying"]
    assert labels_for_windows(annotation, [EPOCH + 17.99], [EPOCH + 19.99]) == [None]


def test_unannotated_time_is_excluded_instead_of_assumed_empty(tmp_path):
    doc = valid_document()
    doc["trials"][0]["empty_intervals"] = [{"start_s": 0, "end_s": 6}, {"start_s": 100, "end_s": 120}]
    annotation = load_event_annotations(write_document(tmp_path, doc), [recorded_session()])[0]
    assert labels_for_windows(annotation, [EPOCH + 8, EPOCH + 94], [EPOCH + 10, EPOCH + 96]) == [None, None]


@pytest.mark.parametrize("unknown", ["pose", "label"])
def test_unknown_pose_or_posture_is_not_given_a_generic_person_label(tmp_path, unknown):
    doc = valid_document()
    if unknown == "pose":
        doc["trials"][0]["events"][0]["pose_s"] = None
    else:
        doc["trials"][0]["events"][0]["label"] = "unclassified-posture"
    annotation = load_event_annotations(write_document(tmp_path, doc), [recorded_session()])[0]
    assert labels_for_windows(annotation, [EPOCH, EPOCH + 20], [EPOCH + 2, EPOCH + 22]) == ["empty", None]


def test_zero_second_settled_pose_can_label_first_window(tmp_path):
    doc = valid_document()
    event = doc["trials"][0]["events"][0]
    event["entered_s"] = event["pose_s"] = 0
    doc["trials"][0]["empty_intervals"] = [{"start_s": 90, "end_s": 120}]
    annotation = load_event_annotations(write_document(tmp_path, doc), [recorded_session()])[0]
    assert labels_for_windows(annotation, [EPOCH], [EPOCH + 2]) == ["lying"]


def test_censored_exit_does_not_label_beyond_observation_end(tmp_path):
    doc = valid_document()
    doc["trials"][0]["events"][0]["exited_s"] = 130
    doc["trials"][0]["empty_intervals"] = [{"start_s": 0, "end_s": 12}]
    annotation = load_event_annotations(write_document(tmp_path, doc), [recorded_session()])[0]
    assert labels_for_windows(annotation, [EPOCH + 118, EPOCH + 120], [EPOCH + 120, EPOCH + 122]) == ["lying", None]


def test_numpy_integer_epoch_sequences_are_supported(tmp_path):
    annotation = load_event_annotations(write_document(tmp_path, valid_document()), [recorded_session()])[0]
    starts = np.array([int(EPOCH), int(EPOCH) + 18], dtype=np.int64)
    assert labels_for_windows(annotation, starts, starts + 2) == ["empty", "lying"]


@pytest.mark.parametrize("starts,ends", [
    ([EPOCH], []), ([], [EPOCH + 2]), ([EPOCH], [EPOCH + 2, EPOCH + 4]),
])
def test_mismatched_window_sequence_lengths_are_rejected(tmp_path, starts, ends):
    annotation = load_event_annotations(write_document(tmp_path, valid_document()), [recorded_session()])[0]
    with pytest.raises(AnnotationError, match="same length"):
        labels_for_windows(annotation, starts, ends)


@pytest.mark.parametrize("starts,ends", [
    ([True], [EPOCH + 2]), ([EPOCH], [False]), ([float("nan")], [EPOCH + 2]),
    ([EPOCH], [float("inf")]), (["1790000000"], [EPOCH + 2]),
    ([EPOCH], [EPOCH]), ([EPOCH + 2], [EPOCH]), (None, []), ("1790000000", [EPOCH + 2]),
])
def test_invalid_window_epochs_are_rejected(tmp_path, starts, ends):
    annotation = load_event_annotations(write_document(tmp_path, valid_document()), [recorded_session()])[0]
    with pytest.raises(AnnotationError):
        labels_for_windows(annotation, starts, ends)


def test_empty_window_sequences_return_no_training_labels(tmp_path):
    annotation = load_event_annotations(write_document(tmp_path, valid_document()), [recorded_session()])[0]
    assert labels_for_windows(annotation, [], []) == []


def test_window_label_origin_must_remain_finite(tmp_path):
    annotation = load_event_annotations(write_document(tmp_path, valid_document()), [recorded_session()])[0]
    invalid = AnnotatedTrial(annotation.trial, annotation.session_ids, float("inf"))
    with pytest.raises(AnnotationError, match="finite number"):
        labels_for_windows(invalid, [EPOCH], [EPOCH + 2])


def settled_document(label="lying"):
    doc = valid_document()
    trial = doc["trials"][0]
    trial["events"] = []
    trial["empty_intervals"] = []
    trial["settled_intervals"] = [{"start_s": 0, "end_s": 120, "label": label}]
    return doc


def test_old_annotation_without_settled_intervals_remains_valid(tmp_path):
    annotation = load_event_annotations(write_document(tmp_path, valid_document()), [recorded_session()])[0]
    assert annotation.trial.settled_intervals == []


def test_explicit_settled_scene_labels_windows_without_inventing_entry(tmp_path):
    annotation = load_event_annotations(write_document(tmp_path, settled_document()), [recorded_session()])[0]
    assert annotation.trial.events == []
    assert annotation.trial.settled_intervals[0].label == "lying"
    assert labels_for_windows(annotation, [EPOCH - 2, EPOCH, EPOCH + 118, EPOCH + 120],
                              [EPOCH, EPOCH + 2, EPOCH + 120, EPOCH + 122]) == [None, "lying", "lying", None]


@pytest.mark.parametrize("label", ["", "unclassified-posture", "fan"])
def test_settled_labels_never_fall_back_to_recording_scene_label(tmp_path, label):
    annotation = load_event_annotations(write_document(tmp_path, settled_document(label)), [recorded_session()])[0]
    assert annotation.trial.events == []
    assert labels_for_windows(annotation, [EPOCH], [EPOCH + 2]) == [None]


def test_optional_settled_label_is_left_unknown(tmp_path):
    doc = settled_document()
    del doc["trials"][0]["settled_intervals"][0]["label"]
    annotation = load_event_annotations(write_document(tmp_path, doc), [recorded_session()])[0]
    assert annotation.trial.settled_intervals[0].label == ""
    assert labels_for_windows(annotation, [EPOCH], [EPOCH + 2]) == [None]


@pytest.mark.parametrize("field,value", [
    ("start_s", True), ("start_s", "0"), ("start_s", float("nan")),
    ("end_s", float("inf")), ("end_s", False), ("label", 7),
])
def test_settled_interval_fields_keep_strict_types(tmp_path, field, value):
    doc = settled_document()
    doc["trials"][0]["settled_intervals"][0][field] = value
    with pytest.raises(AnnotationError, match="finite number|string"):
        load_event_annotations(write_document(tmp_path, doc), [recorded_session()])


@pytest.mark.parametrize("value", [None, {}, "0..120", [None], [[0, 120]]])
def test_settled_interval_container_and_items_are_strict(tmp_path, value):
    doc = settled_document()
    doc["trials"][0]["settled_intervals"] = value
    with pytest.raises(AnnotationError):
        load_event_annotations(write_document(tmp_path, doc), [recorded_session()])


def test_settled_interval_unknown_keys_are_rejected(tmp_path):
    doc = settled_document()
    doc["trials"][0]["settled_intervals"][0]["pose_s"] = 0
    with pytest.raises(AnnotationError, match="unknown keys"):
        load_event_annotations(write_document(tmp_path, doc), [recorded_session()])


@pytest.mark.parametrize("problem", ["reversed", "outside", "settled-overlap", "empty-overlap", "event-pose-overlap"])
def test_core_checks_settled_interval_boundaries_and_fact_overlaps(tmp_path, problem):
    doc = settled_document()
    trial = doc["trials"][0]
    if problem == "reversed":
        trial["settled_intervals"][0].update(start_s=30, end_s=20)
    elif problem == "outside":
        trial["settled_intervals"][0]["start_s"] = -1
    elif problem == "settled-overlap":
        trial["settled_intervals"].append({"start_s": 10, "end_s": 20, "label": "sitting"})
    elif problem == "empty-overlap":
        trial["empty_intervals"] = [{"start_s": 10, "end_s": 20}]
    else:
        trial["events"] = [{"event_id": "entry", "entered_s": 10, "pose_s": 15,
                             "exited_s": 90, "label": "lying"}]
    with pytest.raises(AnnotationError):
        load_event_annotations(write_document(tmp_path, doc), [recorded_session()])


def test_past_entry_with_observed_settled_pose_is_not_replaced_by_scene_start(tmp_path):
    doc = valid_document()
    trial = doc["trials"][0]
    trial["events"][0].update(entered_s=-10, pose_s=-2)
    trial["empty_intervals"] = [{"start_s": 90, "end_s": 120}]
    annotation = load_event_annotations(write_document(tmp_path, doc), [recorded_session()])[0]
    event = annotation.trial.events[0]
    assert event.entered_s == -10 and event.pose_s == -2
    assert labels_for_windows(annotation, [EPOCH], [EPOCH + 2]) == ["lying"]


def test_settled_windows_require_full_interval_and_observation_containment(tmp_path):
    doc = settled_document()
    trial = doc["trials"][0]
    trial["start_s"], trial["end_s"] = 10, 110
    trial["settled_intervals"] = [{"start_s": 20, "end_s": 100, "label": "standing"}]
    annotation = load_event_annotations(write_document(tmp_path, doc), [recorded_session()])[0]
    starts = [EPOCH + t for t in (8, 18, 19, 20, 98, 99, 100, 110)]
    ends = [t + 2 for t in starts]
    assert labels_for_windows(annotation, starts, ends) == [None, None, None, "standing", "standing", None, None, None]


def test_settled_interval_can_follow_an_entry_pose_without_overlap(tmp_path):
    doc = valid_document()
    trial = doc["trials"][0]
    trial["settled_intervals"] = [{"start_s": 90, "end_s": 110, "label": "sitting"}]
    trial["empty_intervals"] = [{"start_s": 0, "end_s": 12}, {"start_s": 110, "end_s": 120}]
    annotation = load_event_annotations(write_document(tmp_path, doc), [recorded_session()])[0]
    starts = [EPOCH + t for t in (88, 90, 108, 110)]
    assert labels_for_windows(annotation, starts, [t + 2 for t in starts]) == ["lying", "sitting", "sitting", "empty"]
