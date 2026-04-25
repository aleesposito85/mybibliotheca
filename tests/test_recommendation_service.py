# tests/test_recommendation_service.py
"""Service-level tests against the in-memory Kuzu fixture."""
import time

import pytest

from app.services.kuzu_recommendation_service import (
    KuzuRecommendationService,
    _count_finished_books,
)


@pytest.fixture
def service():
    return KuzuRecommendationService()


def test_count_finished_books_alice(kuzu_seeded):
    _, ids = kuzu_seeded
    assert _count_finished_books(ids["user_alice"]) == 4


def test_count_finished_books_unknown_user(kuzu_seeded):
    assert _count_finished_books("does-not-exist") == 0


from app.services.kuzu_recommendation_service import (
    _signal_shared_authors,
    _signal_shared_categories,
    _signal_same_series,
    _signal_coreaders,
    _signal_language_match,
)


def test_shared_authors_includes_other_books_by_same_author(kuzu_seeded):
    _, ids = kuzu_seeded
    out = _signal_shared_authors([ids["book_dune"]])
    # Dune is by Herbert; Dune Messiah and Children of Dune share him.
    assert ids["book_dune2"] in out
    assert ids["book_dune3"] in out
    # Foundation (Asimov) does not share author.
    assert ids["book_foundation"] not in out
    # Anchor itself is excluded.
    assert ids["book_dune"] not in out


def test_shared_categories_counts_overlap(kuzu_seeded):
    _, ids = kuzu_seeded
    out = _signal_shared_categories([ids["book_dune"]])
    # dune has [sci_fi, space_opera]; dune2 has [sci_fi, space_opera] -> 2
    assert out[ids["book_dune2"]]["count"] >= 2


def test_same_series_marks_next_volume(kuzu_seeded):
    _, ids = kuzu_seeded
    out = _signal_same_series(ids["book_dune"])
    # Dune is volume 1; volume 2 (Messiah) should be flagged next.
    assert out[ids["book_dune2"]]["next_volume"] is True
    assert out[ids["book_dune2"]]["volume_number"] == 2
    # Volume 3 is in the same series but not the immediate next.
    assert out[ids["book_dune3"]]["next_volume"] is False


def test_coreaders_threshold_floor(kuzu_seeded):
    _, ids = kuzu_seeded
    # Foundation was finished by alice, bob, carol → 3 distinct readers,
    # which IS >= threshold for the raw query (filtering happens in the
    # scorer, but the query itself returns the count).
    out = _signal_coreaders([ids["book_foundation"]])
    # Books read by anyone who also read Foundation:
    assert ids["book_dune"] in out
    # Self-anchor must be excluded.
    assert ids["book_foundation"] not in out


def test_language_match(kuzu_seeded):
    _, ids = kuzu_seeded
    out = _signal_language_match([ids["book_dune"]], candidate_pool={
        ids["book_dune2"], ids["book_foundation"], ids["book_storm1"],
    })
    # All seed books are 'en' so all candidates match.
    assert out == {
        ids["book_dune2"]: True,
        ids["book_foundation"]: True,
        ids["book_storm1"]: True,
    }


from app.services.kuzu_recommendation_service import (
    _recent_finished_anchors,
    _user_library_book_ids,
    _hydrate_books,
    _popular_global,
    _continue_series_for,
)


def test_recent_finished_anchors_orders_by_finish_date_desc(kuzu_seeded):
    _, ids = kuzu_seeded
    anchors = _recent_finished_anchors(ids["user_alice"], limit=5)
    # alice's finishes: foundation(20), dune3(30), dune2(60), dune(90)
    expected = [ids["book_foundation"], ids["book_dune3"], ids["book_dune2"], ids["book_dune"]]
    assert anchors == expected


def test_user_library_excludes_others(kuzu_seeded):
    _, ids = kuzu_seeded
    library = _user_library_book_ids(ids["user_alice"])
    assert ids["book_dune"] in library
    assert ids["book_foundation"] in library
    # Bob's books not in alice's library
    assert ids["book_storm1"] not in library


def test_hydrate_books_returns_display_fields(kuzu_seeded):
    _, ids = kuzu_seeded
    books = _hydrate_books([ids["book_dune"], ids["book_foundation"]])
    assert {b["title"] for b in books} == {"Dune", "Foundation"}
    # Authors are populated from AUTHORED edges.
    titles = {b["title"]: b for b in books}
    assert titles["Dune"]["authors"] == ["Frank Herbert"]


def test_hydrate_preserves_input_order(kuzu_seeded):
    _, ids = kuzu_seeded
    out = _hydrate_books([ids["book_foundation"], ids["book_dune"]])
    assert [b["title"] for b in out] == ["Foundation", "Dune"]


def test_popular_global_orders_by_finish_count(kuzu_seeded):
    _, ids = kuzu_seeded
    pop = _popular_global(limit=5)
    # Most finished in seed: dune (3 readers), foundation (3 readers).
    titles = [b["title"] for b in pop]
    assert "Dune" in titles[:2]
    assert "Foundation" in titles[:2]


def test_continue_series_for_alice(kuzu_seeded):
    _, ids = kuzu_seeded
    out = _continue_series_for(ids["user_alice"], limit=5)
    # alice finished foundation vol 1 only; vol 2 is the next unread.
    titles = {row["next_book"]["title"]: row for row in out}
    assert "Foundation and Empire" in titles
    # alice's most recent series finish is foundation (20 days ago) vs dune3 (30),
    # so foundation continuation should come first.
    assert out[0]["next_book"]["title"] == "Foundation and Empire"


def test_more_like_this_excludes_anchor_and_user_library(kuzu_seeded, service):
    _, ids = kuzu_seeded
    out = service.get_more_like_this_sync(
        book_id=ids["book_dune"], user_id=ids["user_alice"], limit=10,
    )
    titles = [b["title"] for b in out]
    # alice already finished Dune 1/2/3 + Foundation; none of those should appear.
    assert "Dune" not in titles
    assert "Dune Messiah" not in titles
    assert "Foundation" not in titles
    # Each result has a reason string.
    for b in out:
        assert b["recommendation_reason"]


def test_more_like_this_for_bob_surfaces_dune3(kuzu_seeded, service):
    _, ids = kuzu_seeded
    # bob has read dune + dune2 + foundations + storm; dune3 is NOT in his library.
    out = service.get_more_like_this_sync(
        book_id=ids["book_dune"], user_id=ids["user_bob"], limit=10,
    )
    titles = [b["title"] for b in out]
    assert "Children of Dune" in titles


def test_top_picks_falls_back_to_popular_for_cold_user(kuzu_seeded, service):
    _, ids = kuzu_seeded
    # 'newbie' is seeded with no finishes (see _kuzu_seed.py).
    out = service.get_top_picks_sync(user_id=ids["user_newbie"], limit=10)
    # All of them are popular-fallback.
    assert all(b["recommendation_reason"] == "Popular among readers" for b in out)


def test_top_picks_personalized_for_alice(kuzu_seeded, service):
    _, ids = kuzu_seeded
    out = service.get_top_picks_sync(user_id=ids["user_alice"], limit=10)
    # Should include at least one non-popular reason (real signal-driven rec).
    reasons = {b["recommendation_reason"] for b in out}
    assert any(r != "Popular among readers" for r in reasons)


def test_continue_series_sync_returns_books(kuzu_seeded, service):
    _, ids = kuzu_seeded
    out = service.get_continue_series_sync(user_id=ids["user_alice"], limit=5)
    titles = [b["title"] for b in out]
    assert "Foundation and Empire" in titles
    # Continue-series cards always carry the volume reason.
    for b in out:
        assert "Volume" in b["recommendation_reason"] or "Next in" in b["recommendation_reason"]


def test_popular_sync_excludes_user_library(kuzu_seeded, service):
    _, ids = kuzu_seeded
    out = service.get_popular_sync(user_id=ids["user_alice"], limit=20)
    titles = [b["title"] for b in out]
    assert "Dune" not in titles  # alice already has it
    assert "Foundation" not in titles


def test_recommendations_page_bundle_for_alice(kuzu_seeded, service):
    _, ids = kuzu_seeded
    bundle = service.get_recommendations_page_sync(ids["user_alice"])
    assert set(bundle.keys()) == {"top_picks", "continue_series", "popular", "personalized"}
    assert bundle["personalized"] is True
    assert isinstance(bundle["top_picks"], list)
    assert isinstance(bundle["continue_series"], list)
    assert isinstance(bundle["popular"], list)


def test_recommendations_page_bundle_cold_start(kuzu_seeded, service):
    _, ids = kuzu_seeded
    bundle = service.get_recommendations_page_sync(ids["user_newbie"])
    assert bundle["personalized"] is False
    # continue_series is gated behind personalized=True, so always empty here.
    assert bundle["continue_series"] == []
    # top_picks falls back to popular; both should have data from seed.
    assert all(k in bundle for k in ("top_picks", "continue_series", "popular"))
