import json
import re
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = PLUGIN_ROOT / "evals" / "review_cases.json"
CASE_ID_RE = re.compile(r"^[PN][0-9]+$")
ALLOWED_ACTIONS = {
    "patch",
    "extend",
    "guidance",
    "new-skill",
    "duplicate",
    "no-change",
    "reject",
}


def load_cases():
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def test_submission_case_count_and_polarity():
    cases = load_cases()

    assert len(cases) == 8
    assert sum(case["kind"] == "positive" for case in cases) == 5
    assert sum(case["kind"] == "negative" for case in cases) == 3
    assert {case["id"] for case in cases} == {
        "P1",
        "P2",
        "P3",
        "P4",
        "P5",
        "N1",
        "N2",
        "N3",
    }


def test_cases_are_unique_and_complete():
    cases = load_cases()
    ids = [case["id"] for case in cases]

    assert len(ids) == len(set(ids))
    for case in cases:
        assert CASE_ID_RE.fullmatch(case["id"])
        assert case["kind"] in {"positive", "negative"}
        assert case["title"].strip()
        assert case["prompt"].strip()
        assert case["expected_action"] in ALLOWED_ACTIONS
        assert case["must"]
        assert case["must_not"]
        assert all(item.strip() for item in case["must"])
        assert all(item.strip() for item in case["must_not"])


def test_negative_cases_never_expect_an_export_action():
    negative_actions = {
        case["expected_action"]
        for case in load_cases()
        if case["kind"] == "negative"
    }

    assert negative_actions <= {"no-change", "reject"}
