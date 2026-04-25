"""Smoke tests for the /recommendations blueprint via Flask test client."""
import pytest

from app import create_app


@pytest.fixture
def client(kuzu_seeded):
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        yield c


def _login(client, user_id):
    """Bypass login by setting the session user id directly."""
    with client.session_transaction() as sess:
        sess["_user_id"] = user_id
        sess["_fresh"] = True


def test_page_redirects_when_anonymous(client):
    res = client.get("/recommendations/")
    assert res.status_code in (301, 302)
    assert "/auth/login" in res.headers.get("Location", "")


def test_page_renders_for_alice(client, kuzu_seeded):
    _, ids = kuzu_seeded
    _login(client, ids["user_alice"])
    res = client.get("/recommendations/")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert "Top picks for you" in body or "Popular among readers" in body
    assert "Popular" in body


def test_more_like_this_json(client, kuzu_seeded):
    _, ids = kuzu_seeded
    _login(client, ids["user_alice"])
    res = client.get(f"/recommendations/api/more-like-this/{ids['book_dune']}")
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "success"
    assert isinstance(body["data"], list)


def test_library_row_json(client, kuzu_seeded):
    _, ids = kuzu_seeded
    _login(client, ids["user_alice"])
    res = client.get("/recommendations/api/library-row")
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "success"
    assert "data" in body
