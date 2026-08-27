import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processors.candidate_filter import confidence_percent, dedupe_candidates, rejection_reason


NOISE = [
    "MORE CO-OP GAMES",
    "IF YOULIKED",
    "PLAYSTATION/ XBOX /PC",
    "COUCHCO-OP",
    "ONLINECO-OP",
    "Pro-f",
    "Drop",
    "Toggle",
    "New creature data sent to terminal",
    "All singleplayerandlocal co-opgameplayfootagecaptured ingame",
]


def test_known_ocr_noise_is_rejected_before_verification():
    assert all(rejection_reason(value) for value in NOISE)


def test_real_titles_survive_filtering():
    candidates = [{"name": name, "confidence": 0.96} for name in [
        "Bread & Fred",
        "Lethal Company",
        "It Takes Two",
        "Brothers - A Tale of Two Sons",
    ]]
    result = dedupe_candidates(candidates)
    assert [item["name"] for item in result] == [
        "Bread & Fred",
        "Lethal Company",
        "It Takes Two",
        "Brothers - A Tale of Two Sons",
    ]


def test_partial_title_fragments_are_consolidated():
    candidates = [
        {"name": "Brothers", "confidence": 0.96},
        {"name": "A TALE OF TWO SONS", "confidence": 0.96},
        {"name": "Brothers - A Tale of Two Sons", "confidence": 0.96},
    ]
    result = dedupe_candidates(candidates)
    assert len(result) == 1
    assert result[0]["name"] == "Brothers - A Tale of Two Sons"


def test_confidence_accepts_fraction_or_percent_and_clamps():
    assert confidence_percent(1.0) == 100
    assert confidence_percent(0.96) == 96
    assert confidence_percent(96.0) == 96
    assert confidence_percent(10000.0) == 100
