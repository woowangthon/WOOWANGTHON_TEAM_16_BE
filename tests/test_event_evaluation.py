"""Real entry, missing evidence and zone-level denominators are evaluated explicitly."""

from dataclasses import replace

import pytest

from app.ml.event_evaluation import (
    EmptyInterval, EntryEvent, SettledInterval, Trial, Window, evaluate_trial, summarize_trials, validate_trial,
)


def _low_then_high(node="RX", *, entry=20, high=.8, end=100, person=False):
    return [Window(node, t, .95 if person else (0 if t <= entry else high),
                   (0 if t <= entry else high) if person else None)
            for t in range(2, end, 2)]


@pytest.mark.parametrize("source, expected", [("measurement", 4), ("replay", 4), ("alarm", 6), ("manual", 6)])
def test_latency_is_from_observed_entry_and_uses_runtime_thresholds(source, expected):
    trial = Trial("entry", "room", 0, 100, [EntryEvent("person", 20, 80, 25)])
    report = evaluate_trial(trial, _low_then_high(), source=source)
    event = report["events"][0]
    assert event["outcome"] == "timely"
    assert event["latency_s"] == expected
    assert event["detected_at_s"] == 20 + expected
    assert event["stable"]["start_s"] == 25
    assert event["stable"]["end_s"] == 80


def test_person_mode_requires_and_uses_person_prediction():
    trial = Trial("model", "room", 0, 100, [EntryEvent("person", 20, 80, 25)])
    report = evaluate_trial(trial, _low_then_high(high=.9, person=True), person_mode=True)
    assert report["events"][0]["latency_s"] == 6
    assert not report["events"][0]["preexisting_at_entry"]
    with pytest.raises(ValueError, match="requires p_person"):
        evaluate_trial(trial, [Window("RX", 2, .99, quality=.1)], person_mode=True)


def _outcome_report(outcome):
    trial = Trial(outcome, "room", 0, 60, [EntryEvent("entry", 10, 50)])
    rows = [Window("RX", t, 0 if t <= 10 else .9) for t in range(2, 60, 2)]
    options = {}
    if outcome == "late":
        options["timeout_s"] = 3
    elif outcome in {"missed", "censored"}:
        rows = [replace(row, p_change=0) for row in rows]
        if outcome == "censored":
            trial = replace(trial, events=[EntryEvent("entry", 10, 20)])
    elif outcome == "unjudged":
        rows = []
    elif outcome == "preexisting_detection":
        rows = [replace(row, p_change=.9) for row in rows]
    return evaluate_trial(trial, rows, expected_nodes=["RX"], **options)


@pytest.mark.parametrize("outcome", ["timely", "late", "missed", "unjudged", "censored", "preexisting_detection"])
def test_six_event_outcomes_are_disjoint_and_exhaustive(outcome):
    report = _outcome_report(outcome)
    assert report["events"][0]["outcome"] == outcome
    assert report["counts"]["events"] == 1
    assert sum(value for key, value in report["counts"].items() if key != "events") == 1
    if outcome in {"unjudged", "preexisting_detection", "missed", "censored"}:
        assert report["events"][0]["latency_s"] is None


def test_preexisting_alarm_is_not_a_zero_second_success():
    report = _outcome_report("preexisting_detection")
    event = report["events"][0]
    assert event["preexisting_at_entry"] is True
    assert event["latency_s"] is None
    summary = summarize_trials([report])
    assert summary["n_detected"] == 0
    assert summary["timely_rate_all_events"] == 0
    assert summary["median_seconds_to_detect_successes"] is None


def test_preexisting_alarm_needs_a_new_rising_after_known_negative():
    trial = Trial("rising", "room", 0, 40, [EntryEvent("entry", 10, 38)])
    rows = [Window("RX", t, 0 if t in {12, 14} else .9) for t in range(2, 40, 2)]
    event = evaluate_trial(trial, rows)["events"][0]
    assert event["preexisting_at_entry"] is True
    assert event["outcome"] == "timely"
    assert event["latency_s"] == 6


def test_unknown_gap_cannot_turn_preexisting_alarm_into_a_new_success():
    trial = Trial("unknown-gap", "room", 0, 40, [EntryEvent("entry", 10, 38)])
    rows = [Window("RX", t, .9, quality=.1 if t == 12 else 1) for t in range(2, 40, 2)]
    event = evaluate_trial(trial, rows)["events"][0]
    assert event["outcome"] == "preexisting_detection"
    assert event["latency_s"] is None


def test_truncated_exit_is_censored_without_guessing_the_unobserved_end():
    trial = Trial("truncated", "room", 0, 25, [EntryEvent("entry", 10, 100, 12)])
    rows = [Window("RX", t, 0) for t in range(2, 25, 2)]
    event = evaluate_trial(trial, rows)["events"][0]
    assert event["outcome"] == "censored"
    assert event["observation_end_s"] == 25
    assert event["observed_s"] == 15


def test_stable_ratio_includes_unknown_time_and_pose_zero_is_valid():
    trial = Trial("quality", "room", 0, 40, [EntryEvent("entry", 0, 40, 0)])
    report = evaluate_trial(trial, [Window("RX", 2, .9), Window("RX", 4, .9, quality=.1)],
                            stable_after_s=0, exit_margin_s=0)
    stable = report["events"][0]["stable"]
    assert stable["status"] == "evaluated"
    assert stable["total_s"] == 40
    assert stable["detected_s"] == stable["valid_s"] == 2
    assert stable["hold_ratio"] == stable["coverage"] == pytest.approx(.05)
    assert stable["hold_ratio_known"] == 1
    assert report["windows"]["bad_quality_node_windows"] == 1
    assert report["windows"]["missing_zone_slots"] == 18


def test_missing_windows_expire_instead_of_becoming_permanent_positive():
    trial = Trial("stale", "room", 0, 40, [EntryEvent("entry", 0, 40, 0)])
    stable = evaluate_trial(trial, [Window("RX", 2, .9)], stable_after_s=0, exit_margin_s=0)["events"][0]["stable"]
    assert stable["detected_s"] == 6
    assert stable["valid_s"] == 6
    assert stable["hold_ratio"] == pytest.approx(6 / 40)


def test_posture_is_not_inferred_and_short_stable_intervals_are_unavailable():
    unknown = _outcome_report("timely")["events"][0]["stable"]
    assert unknown["status"] == "unavailable" and unknown["reason"] == "missing_pose_time"
    trial = Trial("short", "room", 0, 20, [EntryEvent("entry", 0, 20, 20)])
    stable = evaluate_trial(trial, [Window("RX", 2, .9)])["events"][0]["stable"]
    assert stable["status"] == "unavailable"
    assert stable["hold_ratio"] is None


def test_empty_false_alarm_counts_episodes_seconds_and_carry_in():
    trial = Trial("empty", "room", 0, 60, [EntryEvent("entry", 0, 20, 0)], [EmptyInterval(20, 60)])
    rows = [Window("RX", t, 0 if t in {30, 32, 34} else .9) for t in range(2, 60, 2)]
    empty = evaluate_trial(trial, rows)["empty"]
    assert empty["episodes"] == 2
    assert empty["carry_in_episodes"] == 1
    assert empty["carry_in_high"] is True
    assert empty["total_s"] == empty["valid_s"] == 40
    assert empty["detected_s"] == 34
    assert empty["episodes_per_valid_hour"] == 180
    assert empty["false_positive_ratio_known"] == .85


def test_empty_without_judgments_has_no_fabricated_zero_false_alarm_rate():
    trial = Trial("empty-no-data", "room", 0, 20, empty_intervals=[EmptyInterval(0, 20)])
    report = evaluate_trial(trial, [], expected_nodes=["RX"])
    assert report["empty"]["total_s"] == 20
    assert report["empty"]["valid_s"] == report["empty"]["coverage"] == 0
    assert report["empty"]["episodes_per_valid_hour"] is None
    assert report["windows"]["missing_node_slots"] == 10


def test_unknown_gap_does_not_split_one_false_alarm_into_multiple_episodes():
    trial = Trial("false-gap", "room", 0, 30, empty_intervals=[EmptyInterval(0, 30)])
    rows = [Window("RX", 2, .9), Window("RX", 4, .9, quality=.1), Window("RX", 6, .9), Window("RX", 8, .9)]
    empty = evaluate_trial(trial, rows)["empty"]
    assert empty["episodes"] == 1
    assert empty["detected_s"] == 10
    assert empty["carry_in_high"] is False


def test_multiple_nodes_fuse_one_zone_event_without_multiplying_denominators():
    trial = Trial("zone", "room", 0, 60, [EntryEvent("entry", 20, 50, 25)], [EmptyInterval(0, 20)])
    rows = _low_then_high("A", end=60) + _low_then_high("B", end=60)
    rows = [replace(row, quality=.1) if row.node == "A" else row for row in rows]
    report = evaluate_trial(trial, rows, expected_nodes=["A", "B", "C"])
    assert report["counts"]["events"] == 1
    assert report["events"][0]["latency_s"] == 4
    assert report["empty"]["total_s"] == 20
    assert report["windows"]["nodes"] == 3
    assert report["windows"]["expected_node_slots"] == 90
    assert report["windows"]["missing_node_slots"] == 32
    assert report["windows"]["missing_zone_slots"] == 1


def test_runtime_rounding_and_warming_policy_are_used():
    trial = Trial("rounding", "room", 0, 20, [EntryEvent("entry", 0, 20, 0)])
    report = evaluate_trial(trial, [Window("RX", 2, .4996, quality=.6996)])
    assert report["events"][0]["latency_s"] == 2
    assert report["windows"]["bad_quality_node_windows"] == 0
    low = evaluate_trial(trial, [Window("RX", 2, .1), Window("RX", 4, .1), Window("RX", 6, .1)])
    assert low["segments"][1]["state"] == "warming"
    assert low["segments"][2]["state"] == "not_detected"
    bad = evaluate_trial(trial, [Window("RX", 2, .9, quality=.6994)])
    assert bad["events"][0]["outcome"] == "unjudged"


def test_identical_event_and_window_copies_are_deduplicated():
    event = EntryEvent("one-person", 10, 50, 15)
    original = Trial("duplicates", "room", 0, 60, [event, event], [EmptyInterval(0, 10), EmptyInterval(0, 10)])
    validated = validate_trial(original)
    assert len(original.events) == 2
    assert len(validated.events) == len(validated.empty_intervals) == 1
    row = Window("RX", 12, .9)
    report = evaluate_trial(validated, [row, row])
    assert report["counts"]["events"] == report["windows"]["observed_node_windows"] == 1


@pytest.mark.parametrize("trial", [
    Trial("bad", "room", 0, 20, [EntryEvent("e", float("nan"), 10)]),
    Trial("bad", "room", 0, 20, [EntryEvent("e", -10, 0)]),
    Trial("bad", "room", 0, 20, [EntryEvent("e", 10, 9)]),
    Trial("bad", "room", 0, 20, [EntryEvent("e", 10, 19, 9)]),
    Trial("bad", "room", 0, 20, [EntryEvent("e", 2, 10), EntryEvent("e", 2, 11)]),
    Trial("bad", "room", 0, 20, [EntryEvent("e", 2, 10), EntryEvent("f", 9, 12)]),
    Trial("bad", "room", 0, 20, [EntryEvent("e", 2, 10)], [EmptyInterval(9, 12)]),
    Trial("bad", "room", 0, 20, empty_intervals=[EmptyInterval(0, 10), EmptyInterval(9, 12)]),
])
def test_invalid_or_conflicting_annotation_facts_are_rejected(trial):
    with pytest.raises(ValueError):
        validate_trial(trial)


@pytest.mark.parametrize("row, options", [
    (Window("RX", 2, float("nan")), {}),
    (Window("RX", 2, 1.1), {}),
    (Window("RX", 2, .9, quality=float("inf")), {}),
    (Window("RX", 2, .9, quality=-.1), {}),
    (Window("RX", 21, .9), {}),
    (Window("RX", 2, .9), {"person_mode": True}),
    (Window("RX", 2, .9), {"expected_nodes": ["other"]}),
])
def test_invalid_prediction_data_is_rejected_without_probability_fallback(row, options):
    with pytest.raises(ValueError):
        evaluate_trial(Trial("bad-data", "room", 0, 20), [row], **options)


@pytest.mark.parametrize("options", [{"source": "unknown"}, {"person_mode": "yes"}, {"timeout_s": 0},
                                      {"stale_s": float("nan")}, {"window_s": -1},
                                      {"stable_after_s": -1}, {"exit_margin_s": True}])
def test_invalid_policy_options_are_rejected(options):
    with pytest.raises(ValueError):
        evaluate_trial(Trial("options", "room", 0, 20), [], **options)


def test_unannotated_legacy_data_is_explicitly_unavailable():
    report = evaluate_trial(Trial("legacy", "room", 0, 20), [Window("RX", 2, .9)])
    assert report["status"] == "unavailable"
    assert report["reason"] == "no_actual_event_annotations"
    summary = summarize_trials([report])
    assert summary["median_seconds_to_detect_successes"] is None
    assert summary["timely_rate_all_events"] is None


def test_summary_counts_misses_and_unknowns_next_to_success_only_median():
    names = ["timely", "late", "missed", "unjudged", "censored", "preexisting_detection"]
    reports = [_outcome_report(name) for name in names]
    summary = summarize_trials([*reports, reports[0]])
    assert summary["counts"]["events"] == summary["counts"]["trials"] == 6
    assert all(summary["counts"][name] == 1 for name in names)
    assert summary["n_detected"] == 2
    assert summary["median_seconds_to_detect_successes"] == 4
    assert summary["timely_rate_all_events"] == pytest.approx(1 / 6)
    assert summary["detected_rate_all_events"] == pytest.approx(2 / 6)
    changed = {**reports[0], "reason": "different input"}
    with pytest.raises(ValueError, match="conflicting reports"):
        summarize_trials([reports[0], changed])


def test_partial_trial_uses_prefix_for_carry_in_but_not_observation_denominators():
    trial = Trial("partial", "room", 20, 60, [EntryEvent("entry", 20, 40)], [EmptyInterval(40, 60)])
    rows = [Window("RX", t, .9) for t in range(2, 60, 2)]
    report = evaluate_trial(trial, rows)
    assert report["events"][0]["outcome"] == "preexisting_detection"
    assert report["events"][0]["preexisting_at_entry"] is True
    assert report["segments"][0]["start_s"] == 20
    assert report["windows"]["history_node_windows"] == 9
    assert report["windows"]["observed_node_windows"] == 20
    assert report["empty"]["total_s"] == 20
    assert report["empty"]["carry_in_episodes"] == 1


def test_prefix_is_limited_to_runtime_recent_twelve_rows_per_node():
    trial = Trial("bounded-prefix", "room", 30, 50, empty_intervals=[EmptyInterval(30, 50)])
    rows = [Window("RX", t, .9) for t in (2, 4)]
    rows += [Window("RX", t, .9, quality=.1) for t in range(6, 30, 2)]
    report = evaluate_trial(trial, rows)
    assert report["windows"]["history_node_windows"] == 12
    assert report["windows"]["observed_node_windows"] == 0
    assert report["empty"]["valid_s"] == 0
    assert report["empty"]["episodes_per_valid_hour"] is None


def test_new_detection_exactly_at_entry_is_not_preexisting():
    trial = Trial("exact-entry", "room", 0, 40, [EntryEvent("entry", 10, 38)])
    rows = [Window("RX", t, 0) for t in (2, 4, 6, 8)] + [Window("RX", 10, 1)]
    event = evaluate_trial(trial, rows)["events"][0]
    assert event["preexisting_at_entry"] is False
    assert event["outcome"] == "timely"
    assert event["latency_s"] == 0


def test_decimal_timestamp_roundoff_does_not_turn_deadline_hit_into_late():
    trial = Trial("decimal-deadline", "room", 0, 10, [EntryEvent("entry", 4.1, 9)])
    rows = [Window("RX", 4, 0), Window("RX", 4.2, 1)]
    event = evaluate_trial(trial, rows, timeout_s=.1)["events"][0]
    assert event["latency_s"] == pytest.approx(.1)
    assert event["outcome"] == "timely"


def test_decimal_complete_observation_is_not_mislabeled_as_censored():
    trial = Trial("decimal-observation", "room", 0, 10, [EntryEvent("entry", 4.2, 4.3)])
    rows = [Window("RX", t, 0) for t in (1, 2, 3, 4)]
    event = evaluate_trial(trial, rows, timeout_s=.1)["events"][0]
    assert event["observed_s"] == pytest.approx(.1)
    assert event["outcome"] == "missed"


@pytest.mark.parametrize("extra_seconds, expected", [(0, "timely"), (0.000002, "late")])
def test_epoch_subtraction_precision_preserves_deadline_without_hiding_late_hit(extra_seconds, expected):
    origin = 1_700_000_000.123
    entered = 4.2
    detected = (origin + entered + 30 + extra_seconds) - origin
    trial = Trial("epoch-deadline", "room", 0, 60, [EntryEvent("entry", entered, 55)])
    event = evaluate_trial(trial, [Window("RX", detected, 1)])["events"][0]
    assert event["outcome"] == expected


@pytest.mark.parametrize("shortfall, expected", [(0, "missed"), (0.000002, "censored")])
def test_epoch_observation_precision_distinguishes_complete_and_short_trial(shortfall, expected):
    origin = 1_700_000_000.123
    entered = 4.2
    exited = (origin + entered + 0.1 - shortfall) - origin
    trial = Trial("epoch-observation", "room", 0, 10, [EntryEvent("entry", entered, exited)])
    rows = [Window("RX", t, 0) for t in (1, 2, 3, 4)]
    event = evaluate_trial(trial, rows, timeout_s=0.1)["events"][0]
    assert event["outcome"] == expected


def test_prepared_scene_is_scored_from_scene_start_without_inventing_entry():
    trial = Trial("prepared", "room", 0, 60,
                  settled_intervals=[SettledInterval(0, 60, "lying")])
    rows = [Window("RX", t, .9) for t in range(-2, 62, 2)]
    report = evaluate_trial(trial, rows)
    settled = report["settled_intervals"][0]
    assert report["options"]["stable_after_s"] == report["options"]["exit_margin_s"] == 0
    assert settled["start_s"] == 0 and settled["end_s"] == 60
    assert settled["total_s"] == settled["detected_s"] == settled["valid_s"] == 60
    summary = summarize_trials([report])
    assert summary["stable"]["hold_ratio"] == 1
    assert summary["stable"]["settled_intervals_available"] == 1
    assert summary["stable"]["events_available"] == 0
    assert summary["counts"]["events"] == summary["n_entry_evaluable"] == 0
    assert summary["entry_response_status"] == "unavailable"
    assert summary["median_seconds_to_detect_successes"] is None
    assert summary["timely_rate_all_events"] is None


def test_first_twenty_and_last_ten_seconds_are_not_silently_removed():
    trial = Trial("full-scene", "room", 0, 60,
                  settled_intervals=[SettledInterval(0, 60, "lying")])
    rows = [Window("RX", t, 0 if t < 20 or t >= 50 else 1) for t in range(-4, 62, 2)]
    report = evaluate_trial(trial, rows)
    settled = report["settled_intervals"][0]
    assert settled["total_s"] == 60
    assert 0 < settled["detected_s"] < 60
    assert settled["hold_ratio"] == pytest.approx(settled["detected_s"] / 60)
    assert settled["hold_ratio"] < .8


@pytest.mark.parametrize("history", [False, True])
def test_entry_during_preparation_does_not_become_a_fake_scene_start_detection(history):
    trial = Trial("before-scene", "room", 0, 60, [EntryEvent("entry", -15, 60, -5)])
    rows = [Window("RX", t, .9) for t in range(-18 if history else 2, 62, 2)]
    report = evaluate_trial(trial, rows)
    event = report["events"][0]
    assert event["outcome"] == "censored" and event["reason"] == "entry_before_observation"
    assert event["entry_observed"] is False
    assert event["preexisting_at_entry"] is None
    assert event["latency_s"] is event["detected_at_s"] is None
    assert event["observation_start_s"] == event["stable"]["start_s"] == 0
    assert event["observed_s"] == event["stable"]["total_s"] == 60
    summary = summarize_trials([report])
    assert summary["counts"]["events"] == summary["n_entry_before_observation"] == 1
    assert summary["entry_response_status"] == "unavailable"
    assert summary["timely_rate_all_events"] is None
    assert summary["median_seconds_to_detect_successes"] is None


def test_late_posture_starts_steady_scoring_at_observed_posture_not_twenty_seconds():
    trial = Trial("late-posture", "room", 0, 60, [EntryEvent("entry", -15, 60, 8)])
    event = evaluate_trial(trial, [Window("RX", t, .9) for t in range(-2, 62, 2)])["events"][0]
    assert event["stable"]["start_s"] == 8
    assert event["stable"]["total_s"] == 52


def test_observed_entry_latency_is_unchanged_by_optional_stable_trimming():
    trial = Trial("legacy-trim", "room", 0, 100, [EntryEvent("entry", 20, 80, 25)])
    default = evaluate_trial(trial, _low_then_high())["events"][0]
    trimmed = evaluate_trial(trial, _low_then_high(), stable_after_s=20, exit_margin_s=10)["events"][0]
    assert default["latency_s"] == trimmed["latency_s"] == 4
    assert default["stable"]["start_s"] == 25
    assert default["stable"]["end_s"] == 80
    assert trimmed["stable"]["start_s"] == 40
    assert trimmed["stable"]["end_s"] == 70


def test_mixed_entry_observation_reports_partial_rates_with_explicit_denominator():
    observed = _outcome_report("timely")
    before = evaluate_trial(Trial("earlier", "room", 0, 60, [EntryEvent("entry", -15, 60, -5)]),
                            [Window("RX", t, .9) for t in range(2, 62, 2)])
    summary = summarize_trials([observed, before])
    assert summary["entry_response_status"] == "partial"
    assert summary["counts"]["events"] == 2
    assert summary["n_entry_evaluable"] == summary["n_entry_before_observation"] == 1
    assert summary["timely_rate_all_events"] is None
    assert summary["detected_rate_all_events"] is None
    assert summary["timely_rate_evaluable_events"] == 1


@pytest.mark.parametrize("intervals, events, empty", [
    ([SettledInterval(-1, 10)], [], []),
    ([SettledInterval(0, 21)], [], []),
    ([SettledInterval(10, 10)], [], []),
    ([SettledInterval(0, 10), SettledInterval(9, 20)], [], []),
    ([SettledInterval(0, 10)], [EntryEvent("entry", -2, 5, 0)], []),
    ([SettledInterval(0, 10)], [], [EmptyInterval(5, 20)]),
    ([SettledInterval(0, 10, True)], [], []),
    ([SettledInterval(False, 10)], [], []),
])
def test_settled_interval_facts_cannot_overlap_or_leave_observation(intervals, events, empty):
    with pytest.raises(ValueError):
        validate_trial(Trial("bad-settled", "room", 0, 20, events, empty, intervals))


def test_duplicate_settled_intervals_count_time_only_once():
    interval = SettledInterval(0, 20, "sitting")
    trial = Trial("duplicates-settled", "room", 0, 20, settled_intervals=[interval, interval])
    report = evaluate_trial(trial, [Window("RX", t, .9) for t in range(-2, 22, 2)])
    assert len(report["settled_intervals"]) == 1
    assert summarize_trials([report])["stable"]["total_s"] == 20


def test_explicit_settled_boundaries_are_not_trimmed_even_with_legacy_event_options():
    trial = Trial("explicit-boundaries", "room", 0, 60,
                  settled_intervals=[SettledInterval(0, 60, "lying")])
    report = evaluate_trial(trial, [Window("RX", t, .9) for t in range(-2, 62, 2)],
                            stable_after_s=20, exit_margin_s=10)
    assert report["settled_intervals"][0]["total_s"] == 60


def test_settled_observation_without_signal_is_unjudged_with_zero_coverage():
    trial = Trial("missing-steady", "room", 0, 60,
                  settled_intervals=[SettledInterval(0, 60, "lying")])
    report = evaluate_trial(trial, [], expected_nodes=["RX"])
    assert report["settled_intervals"][0]["status"] == "unjudged"
    summary = summarize_trials([report])
    assert summary["stable"]["status"] == "unjudged"
    assert summary["stable"]["total_s"] == 60
    assert summary["stable"]["valid_s"] == summary["stable"]["coverage"] == 0
    assert summary["stable"]["hold_ratio_known"] is None
