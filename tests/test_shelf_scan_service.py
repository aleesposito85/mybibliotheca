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
