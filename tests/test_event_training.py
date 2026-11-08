"""실제 입장 주석과 독립 검증 결과가 모델 카드까지 연결되는지 확인한다(합성 시험)."""

import json

import pytest
import numpy as np

from app.ml.train import train
from app.ml.evaluate_events import evaluate, main as evaluate_main
from app.ml.event_evaluation import EntryEvent, Trial, Window, evaluate_trial, summarize_trials
from app.ml.event_validation import _score_session
from app.ml.sessions import Session
from app.ml.train import _windows
from synthetic import SyntheticRoom, write_session


def make_data(folder, days=2):
    sessions = []
    for i in range(days):
        day = f"2026-11-{1 + i:02}"
        origin = 1_700_100_000.0 + i * 86400
        room = SyntheticRoom(seed=31)
        for label, seconds, offset in (("empty", 40, 0), ("standing", 60, 60)):
            sid = f"{day}_RX_A_{label}"
            write_session(folder / day, sid, room.frames(seconds, label, seed=i + 2, t0=origin + offset),
                          {"node": "RX-A", "label": label, "day": day, "started_at": origin + offset})
            if label == "standing":
                sessions.append(sid)
    return sessions


def test_missing_entry_annotations_do_not_report_recording_start_as_entry_latency(tmp_path):
    data = tmp_path / "sessions"
    make_data(data)
    card = train(data, tmp_path / "model.joblib")
    evaluation = card["validation"]["event_evaluation"]
    assert evaluation["status"] == "unavailable"
    assert "실제 입장" in evaluation["reason"]
    assert all(fold["median_seconds_to_detect"] is None for fold in card["validation"]["folds"])


def write_annotations(path, session_ids):
    path.write_text(json.dumps({"schema_version": 1, "trials": [
        {"trial_id": f"trial-{i}", "zone_id": "A", "session_ids": [sid], "origin_session_id": sid,
         "start_s": 0, "end_s": 60,
         "events": [{"event_id": "entry-1", "entered_s": 5, "pose_s": 10, "exited_s": 55,
                     "label": "standing"}],
         "empty_intervals": [{"start_s": 0, "end_s": 5}, {"start_s": 55, "end_s": 60}]}
        for i, sid in enumerate(session_ids)]}), encoding="utf-8")
    return path


def write_prepared_annotations(path, session_ids, *, entry_times=False):
    path.write_text(json.dumps({"schema_version": 1, "trials": [
        {"trial_id": f"prepared-{i}", "zone_id": "A", "session_ids": [sid], "origin_session_id": sid,
         "start_s": 0, "end_s": 60, "empty_intervals": [],
         "events": ([{"event_id": "entry-1", "entered_s": -15, "pose_s": -5,
                      "exited_s": 60, "label": "standing"}] if entry_times else []),
         "settled_intervals": ([] if entry_times else [{"start_s": 0, "end_s": 60, "label": "standing"}])}
        for i, sid in enumerate(session_ids)]}), encoding="utf-8")
    return path


def test_prepared_scene_annotations_reach_training_and_model_card_without_extra_trimming(tmp_path):
    data = tmp_path / "sessions"
    ids = make_data(data)
    annotations = write_prepared_annotations(tmp_path / "prepared.json", ids)
    card = train(data, tmp_path / "model.joblib", events_path=annotations)
    for profile in card["validation"]["event_evaluation"]["by_source"].values():
        for report in profile.values():
            assert report["status"] == "available"
            assert report["stable"]["total_s"] == 120
            assert report["stable"]["settled_intervals_available"] == 2
            assert report["counts"]["events"] == 0
            assert report["entry_response_status"] == "unavailable"
            assert report["median_seconds_to_detect_successes"] is None
            assert report["timely_rate_all_events"] is None
            for trial in report["trials"]:
                assert trial["options"]["stable_after_s"] == trial["options"]["exit_margin_s"] == 0
                assert trial["settled_intervals"][0]["start_s"] == 0
                assert trial["settled_intervals"][0]["end_s"] == 60
    for stats in card["data"]["annotation_training_windows"].values():
        # Keep complete two-second windows throughout the minute; a window
        # crossing the annotated end is still excluded by the strict mask.
        assert stats["included_windows"] >= 28


def test_preparation_entry_is_not_scored_as_a_late_detection_in_recorded_scene(tmp_path):
    data = tmp_path / "sessions"
    ids = make_data(data, days=1)
    annotations = write_prepared_annotations(tmp_path / "before-scene.json", ids, entry_times=True)
    report = evaluate(data, annotations)["by_source"]["measurement"]["stage1_only"]
    assert report["status"] == "available"
    assert report["stable"]["total_s"] == 60
    assert report["n_entry_before_observation"] == report["counts"]["censored"] == 1
    assert report["entry_response_status"] == "unavailable"
    assert report["n_detected"] == 0
    assert report["median_seconds_to_detect_successes"] is None
    assert report["timely_rate_all_events"] is None


def test_prepared_scene_cli_reports_steady_duration_and_unavailable_entry_latency(tmp_path, capsys):
    data = tmp_path / "sessions"
    ids = make_data(data, days=1)
    annotations = write_prepared_annotations(tmp_path / "prepared.json", ids)
    output = tmp_path / "report.json"
    assert evaluate_main(["--data", str(data), "--events", str(annotations), "--out", str(output)]) == 0
    console = capsys.readouterr().out
    assert "입장 반응 산출 불가" in console
    assert "자세 유지 관측 60초" in console
    saved = json.loads(output.read_text(encoding="utf-8"))
    steady = saved["by_source"]["measurement"]["stage1_only"]
    assert steady["stable"]["total_s"] == 60
    assert steady["entry_response_status"] == "unavailable"


def test_annotations_feed_held_out_model_card_and_relabel_training_windows(tmp_path):
    data = tmp_path / "sessions"
    ids = make_data(data)
    annotations = write_annotations(tmp_path / "events.json", ids)
    card = train(data, tmp_path / "model.joblib", events_path=annotations)
    evaluation = card["validation"]["event_evaluation"]
    assert evaluation["status"] == "available"
    assert evaluation["reference"] == "observed_entry"
    assert evaluation["annotated_events"] == 2
    assert set(evaluation["by_source"]) == {"measurement", "alarm"}
    for profile, reports in evaluation["by_source"].items():
        for kind in ("model", "stage1_only"):
            report = reports[kind]
            assert report["status"] == "available"
            assert report["counts"]["events"] == 2
            assert report["counts"]["trials"] == 2
            assert len(report["trials"]) == 2
            assert report["thresholds"]["detected"] == (0.5 if profile == "measurement" and kind == "stage1_only" else 0.7)
    for stats in card["data"]["annotation_training_windows"].values():
        assert stats["included_windows"] > 0
        assert stats["excluded_transition_or_unannotated"] > 0
    saved = json.loads((tmp_path / "model.card.json").read_text(encoding="utf-8"))
    assert saved["validation"]["event_evaluation"] == evaluation


def test_one_group_has_no_in_sample_model_event_claim_but_stage1_is_evaluable(tmp_path):
    data = tmp_path / "sessions"
    ids = make_data(data, days=1)
    annotations = write_annotations(tmp_path / "events.json", ids)
    card = train(data, tmp_path / "model.joblib", events_path=annotations)
    evaluation = card["validation"]["event_evaluation"]
    assert evaluation["status"] == "partial"
    profile = evaluation["by_source"]["measurement"]
    assert profile["model"]["status"] == "unavailable"
    assert profile["model"]["unavailable_trials"][0]["events"] == 1
    assert profile["model"]["annotated_events"] == 1
    assert profile["model"]["annotation_event_coverage"] == 0
    assert profile["stage1_only"]["counts"]["events"] == 1


def test_calibration_frames_cannot_be_counted_as_test_empty_time(tmp_path):
    data = tmp_path / "sessions"
    ids = make_data(data)
    annotations = write_annotations(tmp_path / "events.json", [ids[0].replace("standing", "empty")])
    with pytest.raises(ValueError, match="기준선 보정"):
        evaluate(data, annotations)


def test_trial_must_not_mix_training_and_held_out_groups(tmp_path):
    data = tmp_path / "sessions"
    ids = make_data(data)
    # Distinct receiver names make this structurally valid but the days differ.
    meta = next((data / "2026-11-02").glob("*standing.json"))
    value = json.loads(meta.read_text(encoding="utf-8"))
    value["node"] = "RX-B"
    meta.write_text(json.dumps(value), encoding="utf-8")
    annotations = write_annotations(tmp_path / "events.json", [ids[0]])
    value = json.loads(annotations.read_text(encoding="utf-8"))
    value["trials"][0]["session_ids"] = ids
    annotations.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="홀드아웃 그룹"):
        evaluate(data, annotations)


def test_stage1_cli_evaluates_without_model_and_writes_strict_json(tmp_path, capsys):
    data = tmp_path / "sessions"
    ids = make_data(data, days=1)
    annotations = write_annotations(tmp_path / "events.json", ids)
    output = tmp_path / "reports" / "events.json"
    assert evaluate_main(["--data", str(data), "--events", str(annotations), "--out", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["analysis_scope"] == "stage1_only"
    assert report["reference"] == "observed_entry"
    assert report["by_source"]["measurement"]["stage1_only"]["counts"]["events"] == 1
    assert "model" not in report["by_source"]["measurement"]
    assert "전체 1" in capsys.readouterr().out


def test_missing_frames_stay_in_event_denominator_as_unjudged(tmp_path):
    data = tmp_path / "sessions"
    make_data(data, days=1)
    sid = "missing_signal"
    origin = 1_700_100_500.0
    write_session(data, sid, SyntheticRoom(seed=31).frames(60, "standing", fps=1, t0=origin),
                  {"node": "RX-A", "label": "standing", "day": "2026-11-01", "started_at": origin})
    report = evaluate(data, write_annotations(tmp_path / "events.json", [sid]))
    result = report["by_source"]["measurement"]["stage1_only"]
    assert result["counts"]["events"] == 1
    assert result["counts"]["unjudged"] == 1
    assert result["timely_rate_all_events"] == 0
    assert result["trials"][0]["events"][0]["coverage"] == 0


def test_one_group_late_pose_reports_unavailable_validation_instead_of_fitting_one_class(tmp_path):
    data = tmp_path / "sessions"
    ids = make_data(data, days=1)
    annotations = write_annotations(tmp_path / "events.json", ids)
    value = json.loads(annotations.read_text(encoding="utf-8"))
    trial = value["trials"][0]
    trial["events"][0].update(entered_s=45, pose_s=50, exited_s=58)
    trial["empty_intervals"] = [{"start_s": 0, "end_s": 45}, {"start_s": 58, "end_s": 60}]
    annotations.write_text(json.dumps(value), encoding="utf-8")
    card = train(data, tmp_path / "model.joblib", events_path=annotations)
    assert card["validation"]["folds"][0]["model"]["status"] == "unavailable"
    assert card["validation"]["summary"]["recall_at_0.70"] is None
    assert card["validation"]["event_evaluation"]["by_source"]["measurement"]["stage1_only"]["counts"]["events"] == 1


def test_three_entry_example_reports_missed_case_beside_conditional_latency():
    trial = Trial("example", "A", 0, 240, [EntryEvent("one", 10, 60), EntryEvent("two", 90, 140),
                                            EntryEvent("three", 170, 220)], [])
    windows = [Window("RX-A", t, 1.0 if 14 <= t < 60 or 98 <= t < 140 else 0.1)
               for t in range(2, 241, 2)]
    report = summarize_trials([evaluate_trial(trial, windows)])
    assert report["counts"]["events"] == 3
    assert report["counts"]["timely"] == 2
    assert report["counts"]["missed"] == 1
    assert report["n_detected"] == 2
    assert report["median_seconds_to_detect_successes"] == 6
    assert report["timely_rate_all_events"] == pytest.approx(2 / 3)


@pytest.mark.parametrize("scorer", ["events", "training"])
def test_calibration_prefix_is_not_reused_as_ema_or_breathing_context(tmp_path, scorer):
    contexts = []

    class Baseline:
        n_subcarriers = 2
        fps = 10

        def features(self, amp, rssi, ctx_amp, ctx_t):
            contexts.append(ctx_t.copy())
            return np.zeros(8)

        def zscore(self, values):
            return values

        def change_score(self, z):
            return 0.0

        def p_change(self, score):
            return 0.1

    session = Session("split", tmp_path / "split.csv", "RX-A", "empty", "day", {},
                      np.arange(0, 12, 0.1), np.full(120, -45), np.ones((120, 2)))
    if scorer == "events":
        rows = _score_session(session, Baseline(), None, origin=0, start_after=5)
        assert rows[0].t_end == pytest.approx(7)
    else:
        windows, _ = _windows(session, Baseline(), start_after=5)
        assert windows.t_rel[0] == pytest.approx(7)
    assert contexts and all(context.min() >= 5 for context in contexts)


def test_partial_model_report_does_not_present_subset_rate_as_all_entries_rate(tmp_path):
    data = tmp_path / "sessions"
    ids = make_data(data)
    annotations = write_annotations(tmp_path / "events.json", ids)
    from app.ml.sessions import load_sessions
    from app.ml.train import build_day_windows
    from app.ml.event_validation import evaluate_validation_events

    sessions, _ = load_sessions(data)
    _, _, baselines = build_day_windows(sessions)

    class ConstantModel:
        def predict_proba(self, rows):
            return np.tile([0.1, 0.9], (len(rows), 1))

    report, groups = evaluate_validation_events(sessions, baselines, {"2026-11-01": ConstantModel()},
                                                 annotations, {})
    model = report["by_source"]["measurement"]["model"]
    assert model["status"] == "partial"
    assert model["entry_response_status"] == "partial"
    assert model["annotated_events"] == 2
    assert model["counts"]["events"] == 1
    assert model["unavailable_events"] == 1
    assert model["annotation_event_coverage"] == 0.5
    assert model["timely_rate_all_events"] is None
    assert groups["2026-11-02"]["status"] == "partial"


@pytest.mark.parametrize("model_groups", [0, 1])
def test_partial_model_prepared_trials_expose_missing_steady_denominator(tmp_path, model_groups):
    from app.ml.sessions import load_sessions
    from app.ml.train import build_day_windows
    from app.ml.event_validation import evaluate_validation_events

    data = tmp_path / "sessions"
    ids = make_data(data)
    annotations = write_prepared_annotations(tmp_path / "prepared-partial.json", ids)
    sessions, _ = load_sessions(data)
    _, _, baselines = build_day_windows(sessions)

    class ConstantModel:
        def predict_proba(self, rows):
            return np.tile([0.1, 0.9], (len(rows), 1))

    models = {"2026-11-01": ConstantModel()} if model_groups else {}
    report, _ = evaluate_validation_events(sessions, baselines, models, annotations, {})
    model = report["by_source"]["measurement"]["model"]
    assert model["status"] == ("partial" if model_groups else "unavailable")
    assert model["annotated_trials"] == 2
    assert model["unavailable_trial_count"] == 2 - model_groups
    assert model["annotation_trial_coverage"] == model_groups / 2
    assert model["annotated_events"] == model["counts"]["events"] == 0
    assert model["annotation_event_coverage"] is None
    assert model["entry_response_status"] == "unavailable"
    assert model["timely_rate_all_events"] is None
    assert model["stable"]["total_s"] == 60 * model_groups
    assert model["stable"]["scope"] == "evaluated_trials_only"
    stage1 = report["by_source"]["measurement"]["stage1_only"]
    assert stage1["stable"]["total_s"] == 120
    assert stage1["stable"]["scope"] == "all_annotated_trials"
    assert stage1["annotation_trial_coverage"] == 1


@pytest.mark.parametrize("facts", ["same_calibration_file", "different_file_entry", "different_file_settled"])
def test_observed_occupancy_conflicts_with_empty_calibration_even_before_trial(tmp_path, facts):
    data = tmp_path / "sessions"
    ids = make_data(data, days=1)
    if facts == "same_calibration_file":
        sid = ids[0].replace("standing", "empty")
        annotations = write_annotations(tmp_path / "conflict.json", [sid])
        value = json.loads(annotations.read_text(encoding="utf-8"))
        trial = value["trials"][0]
        trial.update(start_s=30, end_s=40, empty_intervals=[])
        trial["events"][0].update(entered_s=10, pose_s=15, exited_s=40)
    elif facts == "different_file_entry":
        annotations = write_annotations(tmp_path / "conflict.json", ids)
        value = json.loads(annotations.read_text(encoding="utf-8"))
        trial = value["trials"][0]
        # The person recording begins 60 s after calibration began; entry at
        # -50 means the observer explicitly places the person in calibration.
        trial.update(empty_intervals=[])
        trial["events"][0].update(entered_s=-50, pose_s=-45, exited_s=55)
    else:
        sid, origin = "overlapping-person", 1_700_100_010.0
        write_session(data, sid, SyntheticRoom(seed=31).frames(60, "standing", seed=5, t0=origin),
                      {"node": "RX-A", "label": "standing", "day": "2026-11-01", "started_at": origin})
        annotations = write_prepared_annotations(tmp_path / "conflict.json", [sid])
        value = json.loads(annotations.read_text(encoding="utf-8"))
    annotations.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="기준선 보정"):
        evaluate(data, annotations)
