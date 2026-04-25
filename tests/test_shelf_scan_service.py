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
