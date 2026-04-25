"""Smoke tests for the /books/scan blueprint via Flask test client."""
import io
from unittest.mock import patch

import pytest
from PIL import Image

from app import create_app


@pytest.fixture
def client(kuzu_seeded):
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        yield c


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = user_id
        sess["_fresh"] = True


def _jpeg_bytes(w=200, h=200):
    img = Image.new("RGB", (w, h), color=(120, 50, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def test_upload_page_redirects_when_anonymous(client):
    res = client.get("/books/scan/")
    assert res.status_code in (301, 302)
    assert "/auth/login" in res.headers.get("Location", "")


def test_upload_page_renders_for_logged_in_user(client, kuzu_seeded):
    _, ids = kuzu_seeded
    _login(client, ids["user_alice"])
    res = client.get("/books/scan/")
    assert res.status_code == 200
    assert b"Scan a bookshelf" in res.data or b"Scan" in res.data


def test_upload_no_file_flashes_and_redirects(client, kuzu_seeded):
    _, ids = kuzu_seeded
    _login(client, ids["user_alice"])
    res = client.post("/books/scan/upload", data={})
    assert res.status_code in (302, 303)


def test_upload_happy_path(client, kuzu_seeded):
    _, ids = kuzu_seeded
    _login(client, ids["user_alice"])
    fake_result = {
        "scan_id": "abc123",
        "candidates": [],
        "summary": {"detected": 0, "matched": 0, "already_owned": 0, "unmatched": 0},
        "preview_url": "/uploads/scans/abc123.jpg",
    }
    with patch("app.routes.shelf_scan_routes.shelf_scan_service.scan_image_and_enrich_sync",
               return_value=fake_result):
        res = client.post(
            "/books/scan/upload",
            data={"shelf_image": (io.BytesIO(_jpeg_bytes()), "shelf.jpg")},
            content_type="multipart/form-data",
        )
    assert res.status_code == 200
    assert b"abc123" in res.data or b"Scan results" in res.data or b"Add" in res.data


def test_upload_llm_unavailable_returns_friendly_error(client, kuzu_seeded):
    from app.services.shelf_scan_service import ShelfScanLLMUnavailable
    _, ids = kuzu_seeded
    _login(client, ids["user_alice"])
    with patch("app.routes.shelf_scan_routes.shelf_scan_service.scan_image_and_enrich_sync",
               side_effect=ShelfScanLLMUnavailable()):
        res = client.post(
            "/books/scan/upload",
            data={"shelf_image": (io.BytesIO(_jpeg_bytes()), "shelf.jpg")},
            content_type="multipart/form-data",
        )
    assert res.status_code == 200
    assert b"Vision model is unavailable" in res.data


def test_confirm_returns_410_for_unknown_scan(client, kuzu_seeded):
    _, ids = kuzu_seeded
    _login(client, ids["user_alice"])
    with patch("app.routes.shelf_scan_routes.shelf_scan_service.get_scan", return_value=None):
        res = client.post("/books/scan/confirm", data={
            "scan_id": "notreal",
            "detection_id": ["det_001"],
        })
    assert res.status_code == 410


def test_confirm_happy_path(client, kuzu_seeded):
    _, ids = kuzu_seeded
    _login(client, ids["user_alice"])
    with patch("app.routes.shelf_scan_routes.shelf_scan_service.get_scan",
               return_value={"user_id": ids["user_alice"], "candidates": []}), \
         patch("app.routes.shelf_scan_routes.shelf_scan_service.start_bulk_add_async",
               return_value="task_xyz"):
        res = client.post("/books/scan/confirm", data={
            "scan_id": "valid",
            "detection_id": ["det_001"],
        })
    body = res.get_json()
    assert res.status_code == 200
    assert body["status"] == "success"
    assert body["task_id"] == "task_xyz"


def test_progress_returns_404_for_unknown_task(client, kuzu_seeded):
    _, ids = kuzu_seeded
    _login(client, ids["user_alice"])
    with patch("app.utils.safe_import_manager.safe_get_import_job", return_value=None):
        res = client.get("/books/scan/progress/notreal")
    assert res.status_code == 404


def test_discard_removes_scan(client, kuzu_seeded):
    _, ids = kuzu_seeded
    _login(client, ids["user_alice"])
    with patch("app.routes.shelf_scan_routes.shelf_scan_service.discard_scan", return_value=True):
        res = client.post("/books/scan/abc/discard")
    assert res.status_code == 200
    assert res.get_json()["status"] == "success"


def test_admin_health_requires_admin(client, kuzu_seeded):
    _, ids = kuzu_seeded
    _login(client, ids["user_alice"])  # alice is not admin
    res = client.get("/admin/scan/health")
    assert res.status_code == 403
