"""Tests for AIService.extract_books_from_shelf_image (mocked HTTP)."""
from unittest.mock import patch, MagicMock

import pytest

from app.services.ai_service import AIService


def _make_service(provider: str = "ollama") -> AIService:
    cfg = {
        "AI_PROVIDER": provider,
        "OLLAMA_BASE_URL": "http://localhost:11434",
        "OLLAMA_MODEL": "llama3.2-vision",
        "OPENAI_API_KEY": "fake-key",
        "OPENAI_MODEL": "gpt-4o-mini",
        "AI_FALLBACK_ENABLED": "false",  # keep tests deterministic
        "AI_TIMEOUT": "5",
        "AI_MAX_TOKENS": "1000",
    }
    return AIService(cfg)


def test_extract_books_from_shelf_image_returns_parsed_list():
    svc = _make_service("ollama")
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "message": {
            "content": '{"books": [{"title": "Dune", "author": "Frank Herbert", "spine_position": 1, "confidence": "high"}]}'
        }
    }
    with patch("app.services.ai_service.requests.post", return_value=fake_resp):
        out = svc.extract_books_from_shelf_image(b"\x00\x01\x02fakeimage")
    assert len(out) == 1
    assert out[0]["title"] == "Dune"
    assert out[0]["confidence"] == "high"


def test_extract_books_from_shelf_image_empty_on_garbage_response():
    svc = _make_service("ollama")
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"message": {"content": "the model rambled"}}
    with patch("app.services.ai_service.requests.post", return_value=fake_resp):
        out = svc.extract_books_from_shelf_image(b"img")
    assert out == []


def test_extract_books_from_shelf_image_returns_empty_on_http_500():
    svc = _make_service("ollama")
    fake_resp = MagicMock()
    fake_resp.status_code = 500
    fake_resp.text = "internal error"
    with patch("app.services.ai_service.requests.post", return_value=fake_resp):
        out = svc.extract_books_from_shelf_image(b"img")
    assert out == []


def test_extract_books_from_shelf_image_uses_openai_when_configured():
    svc = _make_service("openai")
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "choices": [{
            "message": {
                "content": '{"books": [{"title": "Foundation", "author": "Isaac Asimov", "spine_position": 1, "confidence": "high"}]}'
            }
        }]
    }
    with patch("app.services.ai_service.requests.post", return_value=fake_resp) as p:
        out = svc.extract_books_from_shelf_image(b"img")
    assert len(out) == 1
    assert out[0]["title"] == "Foundation"
    # Sanity-check we hit OpenAI not Ollama
    call_url = p.call_args[0][0] if p.call_args.args else p.call_args.kwargs.get("url", "")
    assert "openai" in call_url or "api.openai.com" in call_url


def test_is_configured_true_for_ollama():
    svc = _make_service("ollama")
    assert svc.is_configured() is True


def test_is_configured_false_when_no_provider_configured():
    cfg = {"AI_PROVIDER": "openai", "OPENAI_API_KEY": "", "OLLAMA_BASE_URL": ""}
    svc = AIService(cfg)
    assert svc.is_configured() is False
