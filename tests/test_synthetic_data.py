"""Tests for the synthetic data generator (runs in memory, writes no files)."""
from collections import Counter

import pytest

from scripts.generate_data import APPLICANT_FIELDS, generate


@pytest.fixture(scope="module")
def data():
    """Generate once and share with all tests in this file."""
    watchlist, applicants, truth = generate()
    return {
        "watchlist": watchlist,
        "applicants": applicants,
        "truth": truth,
        "wl_by_no": {w["record_no"]: w for w in watchlist},
        "app_by_ref": {a["reference_no"]: a for a in applicants},
    }


def test_row_counts(data):
    assert len(data["watchlist"]) == 2000
    assert len(data["applicants"]) == 10000
    assert len(data["truth"]) == 10000


def test_category_mix(data):
    counts = Counter(t["category"] for t in data["truth"])
    assert counts == {"TRUE_MATCH": 500, "NAMESAKE": 2500, "UNRELATED": 7000}


def test_same_seed_gives_identical_data():
    assert generate(7) == generate(7)        # reproducible
    assert generate(1)[1] != generate(2)[1]  # a different seed changes the data


def test_applicants_file_has_no_label_columns(data):
    row = data["applicants"][0]
    assert set(row) == set(APPLICANT_FIELDS)
    for forbidden in ("category", "is_true_match", "linked_record_no", "ground_truth", "split"):
        assert forbidden not in row


def test_true_matches_keep_at_least_one_exact_identifier(data):
    for t in data["truth"]:
        if t["category"] != "TRUE_MATCH":
            continue
        a = data["app_by_ref"][t["applicant_reference_no"]]
        w = data["wl_by_no"][t["linked_record_no"]]
        exact = ((a["birthdate"] == w["birthdate"])
                 + (a["birthplace"] == w["birthplace"])
                 + (a["mother_maiden_name"] == w["mother_maiden_name"]))
        assert exact >= 1


def test_namesakes_differ_in_at_least_one_identifier(data):
    for t in data["truth"]:
        if t["category"] != "NAMESAKE":
            continue
        a = data["app_by_ref"][t["applicant_reference_no"]]
        w = data["wl_by_no"][t["linked_record_no"]]
        differs = (a["birthdate"] != w["birthdate"]
                   or a["birthplace"] != w["birthplace"]
                   or a["mother_maiden_name"] != w["mother_maiden_name"])
        assert differs


def test_namesakes_share_the_surname(data):
    for t in data["truth"]:
        if t["category"] == "NAMESAKE":
            a = data["app_by_ref"][t["applicant_reference_no"]]
            w = data["wl_by_no"][t["linked_record_no"]]
            assert a["last_name"] == w["last_name"]


def test_unrelated_have_no_link(data):
    for t in data["truth"]:
        if t["category"] == "UNRELATED":
            assert t["linked_record_no"] == ""


def test_split_is_30_percent_test_per_category(data):
    test_counts = Counter(t["category"] for t in data["truth"] if t["split"] == "test")
    assert test_counts == {"TRUE_MATCH": 150, "NAMESAKE": 750, "UNRELATED": 2100}


def test_all_emails_are_example_dot_com(data):
    assert all(a["email"].endswith("@example.com") for a in data["applicants"])