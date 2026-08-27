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


def test_mixed_reel_content_only_keeps_title_shaped_candidates():
    candidates = [{"name": value, "confidence": 0.96} for value in [
        "MORE CO-OP GAMES",
        "1. BROTHERS: A TALE OF TWO SONS",
        "2. BREAD & FRED",
        "3. LETHAL COMPANY",
        "4. OVERCOOKED 1 & 2",
        "PLAYSTATION / XBOX / PC",
        "Couch Co-op",
        "IF YOULIKED",
        "Drop",
        "Toggle",
    ]]
    result = dedupe_candidates(candidates)
    names = [item["name"] for item in result]
    assert "Brothers: A TALE OF TWO SONS" in names
    assert "BREAD & FRED" in names
    assert "LETHAL COMPANY" in names
    assert "MORE CO-OP GAMES" not in names
    assert "IF YOULIKED" not in names
    assert "PLAYSTATION / XBOX / PC" not in names
    assert "Drop" not in names
    assert "Toggle" not in names


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


def test_numbered_sequels_are_not_collapsed():
    candidates = [
        {"name": "Overcooked", "confidence": 0.96},
        {"name": "Overcooked! 2", "confidence": 0.96},
    ]
    result = dedupe_candidates(candidates)
    assert [item["name"] for item in result] == ["Overcooked", "Overcooked! 2"]


def test_legitimate_short_caption_like_title_survives():
    assert rejection_reason("If Found...") is None


def test_confidence_accepts_fraction_or_percent_and_clamps():
    assert confidence_percent(1.0) == 100
    assert confidence_percent(0.96) == 96
    assert confidence_percent(96.0) == 96
    assert confidence_percent(10000.0) == 100
