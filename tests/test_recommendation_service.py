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
