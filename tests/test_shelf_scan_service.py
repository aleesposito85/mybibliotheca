"""Service-level tests for ShelfScanService (against the Kuzu fixture)."""
import io
import os
from unittest.mock import patch

import pytest
from PIL import Image

from app.services.shelf_scan_service import ShelfScanService


@pytest.fixture
def service():
    return ShelfScanService()


def _make_jpeg(width: int, height: int) -> bytes:
    img = Image.new("RGB", (width, height), color=(50, 100, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _make_png(width: int, height: int) -> bytes:
    img = Image.new("RGB", (width, height), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_preprocess_resizes_large_jpeg(service, tmp_path):
    big = _make_jpeg(4000, 3000)  # > 2048 long-edge
    with patch.object(service, "_uploads_dir", return_value=str(tmp_path)):
        resized_bytes, preview_url = service._preprocess(big, "shelf.jpg", scan_id="test_scan")
    # Resized image should be smaller AND have its longest edge clamped to 2048.
    out = Image.open(io.BytesIO(resized_bytes))
    assert max(out.size) <= 2048
    assert preview_url == "/uploads/scans/test_scan.jpg"
    assert os.path.exists(os.path.join(str(tmp_path), "test_scan.jpg"))


def test_preprocess_keeps_small_jpeg(service, tmp_path):
    small = _make_jpeg(800, 600)
    with patch.object(service, "_uploads_dir", return_value=str(tmp_path)):
        resized_bytes, preview_url = service._preprocess(small, "shelf.jpg", scan_id="t2")
    out = Image.open(io.BytesIO(resized_bytes))
    assert max(out.size) <= 2048
    # It's still JPEG bytes
    assert out.format == "JPEG"


def test_preprocess_accepts_png(service, tmp_path):
    png = _make_png(1024, 768)
    with patch.object(service, "_uploads_dir", return_value=str(tmp_path)):
        resized_bytes, _ = service._preprocess(png, "shelf.png", scan_id="t3")
    # The preview is always saved as JPEG regardless of input format
    out = Image.open(io.BytesIO(resized_bytes))
    assert out.format == "JPEG"


def test_preprocess_rejects_bad_format(service, tmp_path):
    not_an_image = b"this is plainly not an image"
    with pytest.raises(ValueError):
        with patch.object(service, "_uploads_dir", return_value=str(tmp_path)):
            service._preprocess(not_an_image, "fake.jpg", scan_id="t4")


# ---- Task 6: Scan store + rate limiter + in-flight tracker ---------------

import time as _time

from app.services.shelf_scan_service import (
    SCAN_STORE_TTL_SECONDS,
    ShelfScanRateLimited,
)


def test_scan_store_get_returns_none_for_unknown_id(service):
    assert service.get_scan("does-not-exist", "user_alice") is None


def test_scan_store_get_returns_payload_for_owner(service):
    service._save_scan("scan_a", "user_alice", [{"detection_id": "d1"}])
    out = service.get_scan("scan_a", "user_alice")
    assert out is not None
    assert out["candidates"] == [{"detection_id": "d1"}]


def test_scan_store_get_rejects_non_owner(service):
    service._save_scan("scan_a", "user_alice", [{"detection_id": "d1"}])
    assert service.get_scan("scan_a", "user_bob") is None


def test_scan_store_get_purges_expired(service):
    service._save_scan("scan_a", "user_alice", [{"detection_id": "d1"}])
    # Force expiry by editing the stored expires_at.
    with service._scan_store_lock:
        service._scan_store["scan_a"]["expires_at"] = _time.time() - 1
    assert service.get_scan("scan_a", "user_alice") is None
    # And the entry is gone.
    with service._scan_store_lock:
        assert "scan_a" not in service._scan_store


def test_rate_limiter_allows_under_threshold(service):
    # Default daily limit is 30. Record 5 scans for alice and verify no raise.
    for _ in range(5):
        service._record_scan_for_rate_limit("user_alice")


def test_rate_limiter_blocks_over_threshold(monkeypatch, service):
    monkeypatch.setattr("app.services.shelf_scan_service.DAILY_SCAN_LIMIT_PER_USER", 3)
    for _ in range(3):
        service._record_scan_for_rate_limit("user_x")
    with pytest.raises(ShelfScanRateLimited):
        service._record_scan_for_rate_limit("user_x")


def test_rate_limiter_drops_old_timestamps(monkeypatch, service):
    monkeypatch.setattr("app.services.shelf_scan_service.DAILY_SCAN_LIMIT_PER_USER", 2)
    # Insert two stale entries (older than 24h).
    with service._rate_limit_lock:
        service._rate_limit["user_y"] = [_time.time() - 90000, _time.time() - 86500]
    # New scan should succeed because the stale ones get evicted.
    service._record_scan_for_rate_limit("user_y")


def test_in_flight_marker_blocks_concurrent_scan(service):
    service._mark_scan_in_flight("user_alice")
    with pytest.raises(Exception) as excinfo:
        service._mark_scan_in_flight("user_alice")
    assert "in progress" in str(excinfo.value).lower()
    service._clear_scan_in_flight("user_alice")
    # After clearing, can mark again.
    service._mark_scan_in_flight("user_alice")
    service._clear_scan_in_flight("user_alice")


# ---- Task 7: Enrichment helper -------------------------------------------

from unittest.mock import patch


def test_enrich_one_returns_matched_when_metadata_found(service):
    fake_metadata = [
        {"title": "Dune", "authors": ["Frank Herbert"], "isbn13": "9780441172719",
         "isbn10": "0441172717", "cover_url": "http://x", "published_date": "1990-01-01",
         "page_count": 535, "language": "en", "description": "...",
         "similarity_score": 0.95},
        {"title": "Dune (Special Edition)", "authors": ["Frank Herbert"], "isbn13": "9780441013593",
         "isbn10": "0441013597", "cover_url": "http://y", "published_date": "2005-04-05",
         "page_count": 600, "language": "en", "description": "...",
         "similarity_score": 0.85},
    ]
    detection = {"title": "Dune", "author": "Frank Herbert", "spine_position": 1, "confidence": "high"}
    with patch("app.services.shelf_scan_service.fetch_unified_by_title", return_value=fake_metadata):
        candidate = service._enrich_one(detection, detection_id="det_001")
    assert candidate["matched"] is True
    assert candidate["best_match"]["isbn13"] == "9780441172719"
    assert len(candidate["alternatives"]) == 1
    assert candidate["alternatives"][0]["isbn13"] == "9780441013593"
    assert candidate["default_selected"] is True   # high+matched
    assert candidate["spine_position"] == 1
    assert candidate["detection_id"] == "det_001"


def test_enrich_one_unmatched(service):
    detection = {"title": "Nonexistent", "author": "", "spine_position": 2, "confidence": "low"}
    with patch("app.services.shelf_scan_service.fetch_unified_by_title", return_value=[]):
        candidate = service._enrich_one(detection, detection_id="det_002")
    assert candidate["matched"] is False
    assert candidate["best_match"] is None
    assert candidate["alternatives"] == []
    assert candidate["default_selected"] is False


def test_enrich_one_low_confidence_unselected_even_when_matched(service):
    fake = [{"title": "Dune", "authors": ["Frank Herbert"], "isbn13": "X",
             "isbn10": None, "cover_url": "", "published_date": "",
             "page_count": None, "language": "en", "description": "",
             "similarity_score": 0.6}]
    detection = {"title": "Dune", "author": "FH", "spine_position": 1, "confidence": "low"}
    with patch("app.services.shelf_scan_service.fetch_unified_by_title", return_value=fake):
        candidate = service._enrich_one(detection, detection_id="d")
    assert candidate["matched"] is True
    assert candidate["default_selected"] is False  # low confidence


def test_enrich_one_caps_alternatives_at_4(service):
    fake = [
        {"title": f"Dune Edition {i}", "authors": ["Frank Herbert"],
         "isbn13": str(9780000000000 + i), "isbn10": None, "cover_url": "",
         "published_date": "", "page_count": None, "language": "en",
         "description": "", "similarity_score": 0.9 - i * 0.01}
        for i in range(10)
    ]
    detection = {"title": "Dune", "author": "FH", "spine_position": 1, "confidence": "high"}
    with patch("app.services.shelf_scan_service.fetch_unified_by_title", return_value=fake):
        candidate = service._enrich_one(detection, detection_id="d")
    assert len(candidate["alternatives"]) == 4


# ---- Task 8: scan_image_and_enrich_sync orchestrator ---------------------

from app.services.shelf_scan_service import (
    ShelfScanLLMUnavailable,
    ShelfScanEmptyResult,
)


def _detect(title, author="", spine=1, conf="high"):
    return {"title": title, "author": author, "spine_position": spine, "confidence": conf}


def _meta(title, isbn13, score=0.95):
    return [{"title": title, "authors": ["X"], "isbn13": isbn13, "isbn10": None,
             "cover_url": "", "published_date": "", "page_count": None,
             "language": "en", "description": "", "similarity_score": score}]


def test_scan_happy_path(kuzu_seeded, service, tmp_path):
    _, ids = kuzu_seeded
    with patch.object(service, "_uploads_dir", return_value=str(tmp_path)), \
         patch("app.services.shelf_scan_service.AIService") as AICls, \
         patch("app.services.shelf_scan_service.fetch_unified_by_title") as ft:
        AICls.return_value.is_configured.return_value = True
        AICls.return_value.extract_books_from_shelf_image.return_value = [
            _detect("Dune", "Frank Herbert", 1),
            _detect("Foundation", "Isaac Asimov", 2),
        ]
        ft.side_effect = [
            _meta("Dune", "9780441172719"),
            _meta("Foundation", "9780553293357"),
        ]
        result = service.scan_image_and_enrich_sync(
            image_bytes=_make_jpeg(800, 600),
            user_id=ids["user_newbie"],     # newbie has no books → nothing filtered
            original_filename="shelf.jpg",
        )
    assert result["scan_id"]
    assert len(result["candidates"]) == 2
    assert result["summary"]["detected"] == 2
    assert result["summary"]["matched"] == 2


def test_scan_filters_already_owned(kuzu_seeded, service, tmp_path):
    _, ids = kuzu_seeded
    with patch.object(service, "_uploads_dir", return_value=str(tmp_path)), \
         patch("app.services.shelf_scan_service.AIService") as AICls, \
         patch("app.services.shelf_scan_service.fetch_unified_by_title") as ft, \
         patch.object(service, "_user_owned_book_titles") as owned:
        AICls.return_value.is_configured.return_value = True
        AICls.return_value.extract_books_from_shelf_image.return_value = [
            _detect("Dune", "Frank Herbert", 1),
            _detect("Foundation", "Isaac Asimov", 2),
        ]
        ft.side_effect = [
            _meta("Dune", "9780441172719"),
            _meta("Foundation", "9780553293357"),
        ]
        # Alice already owns Dune in our fixture; mark its isbn as owned.
        owned.return_value = {"9780441172719"}
        result = service.scan_image_and_enrich_sync(
            image_bytes=_make_jpeg(800, 600),
            user_id=ids["user_alice"],
            original_filename="shelf.jpg",
        )
    titles = [c["best_match"]["title"] for c in result["candidates"] if c.get("best_match")]
    assert "Dune" not in titles
    assert "Foundation" in titles
    assert result["summary"]["already_owned"] == 1


def test_scan_raises_when_llm_unconfigured(kuzu_seeded, service, tmp_path):
    _, ids = kuzu_seeded
    with patch.object(service, "_uploads_dir", return_value=str(tmp_path)), \
         patch("app.services.shelf_scan_service.AIService") as AICls:
        AICls.return_value.is_configured.return_value = False
        with pytest.raises(ShelfScanLLMUnavailable):
            service.scan_image_and_enrich_sync(
                image_bytes=_make_jpeg(400, 300),
                user_id=ids["user_alice"],
                original_filename="shelf.jpg",
            )


def test_scan_raises_when_no_books_detected(kuzu_seeded, service, tmp_path):
    _, ids = kuzu_seeded
    with patch.object(service, "_uploads_dir", return_value=str(tmp_path)), \
         patch("app.services.shelf_scan_service.AIService") as AICls:
        AICls.return_value.is_configured.return_value = True
        AICls.return_value.extract_books_from_shelf_image.return_value = []
        with pytest.raises(ShelfScanEmptyResult) as e:
            service.scan_image_and_enrich_sync(
                image_bytes=_make_jpeg(400, 300),
                user_id=ids["user_alice"],
                original_filename="shelf.jpg",
            )
        # Error carries a preview URL so the upload page can re-render with it.
        assert e.value.preview_url.startswith("/uploads/scans/")


def test_scan_id_isolation_between_users(kuzu_seeded, service, tmp_path):
    _, ids = kuzu_seeded
    with patch.object(service, "_uploads_dir", return_value=str(tmp_path)), \
         patch("app.services.shelf_scan_service.AIService") as AICls, \
         patch("app.services.shelf_scan_service.fetch_unified_by_title") as ft:
        AICls.return_value.is_configured.return_value = True
        AICls.return_value.extract_books_from_shelf_image.return_value = [_detect("X")]
        ft.return_value = _meta("X", "1")
        result = service.scan_image_and_enrich_sync(
            image_bytes=_make_jpeg(400, 300),
            user_id=ids["user_alice"],
            original_filename="shelf.jpg",
        )
    # Bob can't fetch alice's scan.
    assert service.get_scan(result["scan_id"], ids["user_bob"]) is None
    # Alice can.
    assert service.get_scan(result["scan_id"], ids["user_alice"]) is not None


# ---- Task 9: start_bulk_add_async + bulk-add worker ----------------------

def test_bulk_add_creates_books_and_returns_task_id(kuzu_seeded, service):
    _, ids = kuzu_seeded
    # Stash a scan with two candidates.
    candidates = [
        {
            "detection_id": "det_001",
            "matched": True,
            "best_match": {
                "title": "BulkBook 1", "authors": ["A"], "isbn13": "9999000001", "isbn10": None,
                "cover_url": "", "published_date": "", "page_count": None, "language": "en",
                "description": "", "similarity_score": 0.9,
            },
            "alternatives": [],
        },
        {
            "detection_id": "det_002",
            "matched": True,
            "best_match": {
                "title": "BulkBook 2", "authors": ["B"], "isbn13": "9999000002", "isbn10": None,
                "cover_url": "", "published_date": "", "page_count": None, "language": "en",
                "description": "", "similarity_score": 0.9,
            },
            "alternatives": [],
        },
    ]
    service._save_scan("test_scan_bulk", ids["user_newbie"], candidates)

    with patch.object(service, "_create_and_link_book") as cl:
        cl.side_effect = ["book_id_1", "book_id_2"]
        task_id = service.start_bulk_add_async(
            user_id=ids["user_newbie"],
            scan_id="test_scan_bulk",
            picked=["det_001", "det_002"],
            overrides={},
        )
    assert task_id is not None
    # Wait briefly for the worker thread to finish.
    _time.sleep(0.5)
    from app.utils.safe_import_manager import safe_get_import_job
    job = safe_get_import_job(ids["user_newbie"], task_id)
    assert job is not None
    assert job["status"] == "completed"
    assert job["success"] == 2
    assert job["source"] == "shelf_scan"


def test_bulk_add_continues_on_per_book_failure(kuzu_seeded, service):
    _, ids = kuzu_seeded
    candidates = [
        {"detection_id": "ok", "matched": True, "best_match": {
            "title": "Good", "authors": [], "isbn13": "G", "isbn10": None,
            "cover_url": "", "published_date": "", "page_count": None, "language": "en",
            "description": "", "similarity_score": 0.9,
        }, "alternatives": []},
        {"detection_id": "bad", "matched": True, "best_match": {
            "title": "Bad", "authors": [], "isbn13": "B", "isbn10": None,
            "cover_url": "", "published_date": "", "page_count": None, "language": "en",
            "description": "", "similarity_score": 0.9,
        }, "alternatives": []},
    ]
    service._save_scan("test_scan_partial", ids["user_newbie"], candidates)

    def fake_create(user_id, candidate_metadata):
        if candidate_metadata["title"] == "Bad":
            raise RuntimeError("simulated DB failure")
        return "ok_id"

    with patch.object(service, "_create_and_link_book", side_effect=fake_create):
        task_id = service.start_bulk_add_async(
            user_id=ids["user_newbie"],
            scan_id="test_scan_partial",
            picked=["ok", "bad"],
            overrides={},
        )
    _time.sleep(0.5)
    from app.utils.safe_import_manager import safe_get_import_job
    job = safe_get_import_job(ids["user_newbie"], task_id)
    assert job["status"] == "completed"
    assert job["success"] == 1
    assert job["errors"] == 1
    assert len(job["error_messages"]) == 1


def test_bulk_add_uses_override_alternative(kuzu_seeded, service):
    _, ids = kuzu_seeded
    candidate = {
        "detection_id": "det_001",
        "matched": True,
        "best_match": {"title": "Default", "authors": [], "isbn13": "DEFAULT",
                       "isbn10": None, "cover_url": "", "published_date": "",
                       "page_count": None, "language": "en", "description": "",
                       "similarity_score": 0.9},
        "alternatives": [
            {"title": "Alt0", "authors": [], "isbn13": "ALT0", "isbn10": None,
             "cover_url": "", "published_date": "", "page_count": None, "language": "en",
             "description": "", "similarity_score": 0.85},
            {"title": "Alt1", "authors": [], "isbn13": "ALT1", "isbn10": None,
             "cover_url": "", "published_date": "", "page_count": None, "language": "en",
             "description": "", "similarity_score": 0.80},
        ],
    }
    service._save_scan("test_scan_override", ids["user_newbie"], [candidate])

    captured = {}

    def fake_create(user_id, candidate_metadata):
        captured["isbn13"] = candidate_metadata["isbn13"]
        return "ok"

    with patch.object(service, "_create_and_link_book", side_effect=fake_create):
        task_id = service.start_bulk_add_async(
            user_id=ids["user_newbie"],
            scan_id="test_scan_override",
            picked=["det_001"],
            overrides={"det_001": 1},   # 1 means alternatives[1]
        )
    _time.sleep(0.5)
    assert captured["isbn13"] == "ALT1"
