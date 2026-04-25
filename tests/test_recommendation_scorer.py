"""Unit tests for the pure recommendation scorer (no DB)."""
from app.services.kuzu_recommendation_service import (
    RECS_WEIGHTS,
    COREADER_MIN_THRESHOLD,
    score_candidates,
)


def test_empty_input_returns_empty():
    assert score_candidates({}, anchor_titles={}) == []


def test_single_signal_ranks_lower_than_multi_signal():
    # b1 has 1 author overlap; b2 has 1 author overlap + 2 categories overlap
    signals = {
        "b1": {"author": 1, "category": 0, "series": 0, "coreader": 0, "language": 0},
        "b2": {"author": 1, "category": 2, "series": 0, "coreader": 0, "language": 0},
    }
    ranked = score_candidates(signals, anchor_titles={})
    assert [c["book_id"] for c in ranked] == ["b2", "b1"]
    assert ranked[0]["score"] > ranked[1]["score"]


def test_coreader_threshold_filters_low_counts():
    signals = {
        "b1": {"author": 0, "category": 0, "series": 0, "coreader": 2, "language": 0},
        "b2": {"author": 0, "category": 0, "series": 0, "coreader": 5, "language": 0},
    }
    ranked = score_candidates(signals, anchor_titles={})
    # b1 falls below the threshold and has no other signal → dropped entirely.
    # b2 keeps the coreader signal.
    assert [c["book_id"] for c in ranked] == ["b2"]


def test_coreader_below_threshold_does_not_drop_when_other_signals_present():
    signals = {
        "b1": {"author": 1, "category": 0, "series": 0, "coreader": 2, "language": 0},
    }
    ranked = score_candidates(signals, anchor_titles={})
    assert len(ranked) == 1
    # Score from author only; coreader contribution dropped because <3.
    assert ranked[0]["score"] == RECS_WEIGHTS["author"] * 1


def test_series_signal_dominates_reason():
    signals = {
        "b1": {"author": 0, "category": 0, "series": 1, "series_next_volume": 0,
               "coreader": 0, "language": 0, "series_name": "Foundation",
               "anchor_id": "anchor1"},
    }
    ranked = score_candidates(signals, anchor_titles={"anchor1": "Foundation Book 1"})
    assert ranked[0]["recommendation_reason"] == "Same series as Foundation Book 1"


def test_series_next_volume_uses_volume_phrasing():
    signals = {
        "b1": {"author": 0, "category": 0, "series": 1, "series_next_volume": 1,
               "coreader": 0, "language": 0, "series_name": "Dune",
               "next_volume_number": 2, "anchor_id": "anchor1"},
    }
    ranked = score_candidates(signals, anchor_titles={"anchor1": "Dune"})
    assert ranked[0]["recommendation_reason"] == "Volume 2 of Dune"


def test_author_dominant_reason_uses_author_name():
    signals = {
        "b1": {"author": 1, "category": 0, "series": 0, "coreader": 0,
               "language": 0, "top_author_name": "Frank Herbert"},
    }
    ranked = score_candidates(signals, anchor_titles={})
    assert ranked[0]["recommendation_reason"] == "By Frank Herbert"


def test_category_dominant_reason_uses_category_name():
    signals = {
        "b1": {"author": 0, "category": 3, "series": 0, "coreader": 0,
               "language": 0, "top_category_name": "Science Fiction"},
    }
    ranked = score_candidates(signals, anchor_titles={})
    assert ranked[0]["recommendation_reason"] == "More Science Fiction"


def test_coreader_dominant_reason_uses_anchor_title():
    signals = {
        "b1": {"author": 0, "category": 0, "series": 0, "coreader": 50,
               "language": 0, "anchor_id": "anchor1"},
    }
    ranked = score_candidates(signals, anchor_titles={"anchor1": "Project Hail Mary"})
    assert ranked[0]["recommendation_reason"] == "Read by people who liked Project Hail Mary"


def test_language_signal_never_dominates():
    # Language alone shouldn't produce a reason; book gets dropped entirely
    # because nothing meaningful contributes.
    signals = {
        "b1": {"author": 0, "category": 0, "series": 0, "coreader": 0,
               "language": 1},
    }
    ranked = score_candidates(signals, anchor_titles={})
    # language=0.5 alone is below the meaningful-signal floor; we drop it.
    assert ranked == []
