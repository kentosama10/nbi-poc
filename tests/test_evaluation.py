"""Unit tests for the evaluation maths and report (no databases needed)."""
import inspect

import pytest

from scripts import evaluate


def person(ref, first, last):
    return {"reference_no": ref, "first_name": first, "last_name": last}


def answer(category, linked=""):
    return {"category": category, "linked_record_no": linked,
            "is_true_match": "1" if category == "TRUE_MATCH" else "0"}


def tiny():
    """Six applicants where every expected number can be counted by hand."""
    applicants = [person("A1", "Jose", "Santos"), person("A2", "Maria", "Cruz"),
                  person("A3", "Juan", "Dela"), person("A4", "Ana", "Reyes"),
                  person("A5", "Mark", "Garcia"), person("A6", "Zed", "Zulu")]
    scores = {
        "A1": [{"record_no": "WL-1", "p": 0.99, "band": "ESCALATE"}],     # true, caught
        "A2": [{"record_no": "WL-2", "p": 0.10, "band": "AUTO_CLEAR"}],   # true, WRONGLY cleared
        "A3": [],                                                         # true, not retrieved
        "A4": [{"record_no": "WL-4", "p": 0.05, "band": "AUTO_CLEAR"}],   # namesake, cleared
        "A5": [{"record_no": "WL-5", "p": 0.50, "band": "REVIEW"}],       # namesake, queued
        "A6": [],                                                         # unrelated
    }
    truth = {"A1": answer("TRUE_MATCH", "WL-1"), "A2": answer("TRUE_MATCH", "WL-2"),
             "A3": answer("TRUE_MATCH", "WL-3"), "A4": answer("NAMESAKE", "WL-4"),
             "A5": answer("NAMESAKE", "WL-5"), "A6": answer("UNRELATED")}
    name_index = {("jose", "santos"): {"WL-1"}, ("ana", "reyes"): {"WL-4"}}
    return applicants, scores, truth, name_index


def graded():
    applicants, scores, truth, name_index = tiny()
    g = evaluate.grade(scores, applicants, truth, name_index)
    g["miss_rate_upper_bound"] = evaluate.miss_rate_upper_bound(g["wrongly_cleared"], g["true_total"])
    return g


def fake_results(g):
    return {
        "generated_at": "2026-01-01T00:00:00+00:00", "seed": 42,
        "model": {"version": "logreg-v1", "trained_at": "2026-01-01", "auto_clear": 0.2,
                  "escalate": 0.8, "train_applicants": 7000},
        "data": {"watchlist": 2000, "applicants": 10000, "test_applicants": 6},
        "offline": g,
        "concurrency": {"rounds": [{"succeeded": 1, "slot_full": 49, "other": [],
                                    "booked_in_postgres": 1, "seconds": 1.2}] * 5,
                        "overbooked_seats": 0, "apps_multi": 0, "overbooked_slots": 0,
                        "double_bookings": 0, "clean_rounds": 5},
        "payment": {"runs": 20, "delays": [0.1] * 20, "avg": 0.1, "median": 0.1, "p95": 0.2,
                    "max": 0.3, "within_5s": 20, "consistent": 20, "failures": []},
        "audit": {"steps": [{"label": "GET /x", "expected": 1, "observed": 1,
                             "status": 200, "ok": True}],
                  "coverage": 1.0, "masking_ok": True, "pii_found": [], "denied_rows": 6},
        "analytics": {"ok": True, "missing": [], "sections": ["hit", "queue"]},
        "live_seconds": 30.0, "offline_seconds": 120.0,
    }


def test_upper_bound_for_zero_misses_is_about_three_over_n():
    assert evaluate.miss_rate_upper_bound(0, 150) == pytest.approx(0.0198, abs=0.001)
    assert evaluate.miss_rate_upper_bound(0, 0) is None


def test_upper_bound_with_misses_is_above_the_observed_rate():
    ub = evaluate.miss_rate_upper_bound(3, 100)
    assert 0.03 < ub < 0.10


def test_grade_counts_everything_correctly():
    g = graded()
    assert g["applications"] == 6 and g["flagged"] == 4 and g["need_review"] == 2
    assert g["avoided"] == 2 and g["auto_clear_rate"] == 0.5
    assert (g["pairs"], g["pairs_auto_clear"], g["pairs_review"], g["pairs_escalate"]) == (4, 2, 1, 1)
    assert g["true_total"] == 3 and g["true_caught"] == 1
    assert g["true_cleared"] == 1 and g["true_not_found"] == 1 and g["wrongly_cleared"] == 2
    assert g["recall"] == pytest.approx(1 / 3, abs=1e-3) and g["precision"] == 0.5
    assert g["lowest_true_p"] == 0.10 and g["highest_cleared_p"] == 0.10
    assert g["b_flagged"] == 2 and g["b_caught"] == 1       # baseline B: exact names
    assert g["by_category"]["NAMESAKE"] == {"applicants": 2, "flagged": 2, "need_review": 1}


def test_scoring_function_cannot_see_the_answer_key():
    params = set(inspect.signature(evaluate.score_applicants).parameters)
    assert params == {"applicants", "wl_by_no", "model"}      # no truth, no labels


def test_report_flags_failures_when_a_true_match_is_wrongly_cleared():
    md = evaluate.render_markdown(fake_results(graded()))
    assert "NOT every target passed" in md
    assert "FAIL" in md and "True matches wrongly cleared" in md


def test_report_passes_when_every_target_is_met():
    g = graded()
    g.update(wrongly_cleared=0, true_cleared=0, true_not_found=0, true_caught=3,
             recall=1.0, auto_clear_rate=0.9, miss_rate_upper_bound=0.0198)
    md = evaluate.render_markdown(fake_results(g))
    assert "every checked target passed" in md
    assert "FAIL" not in md
    assert "| Pain point | Test | Target | Result |" in md
    assert len(evaluate.build_findings(fake_results(g))) == 6