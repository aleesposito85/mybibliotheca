"""Unit tests for the shelf-scan LLM response parser (no DB, no network)."""
from app.services.shelf_scan_service import _parse_shelf_response


def test_happy_path_two_books():
    raw = '{"books": [{"title": "Dune", "author": "Frank Herbert", "spine_position": 1, "confidence": "high"}, {"title": "Foundation", "author": "Isaac Asimov", "spine_position": 2, "confidence": "high"}]}'
    out = _parse_shelf_response(raw)
    assert len(out) == 2
    assert out[0]["title"] == "Dune"
    assert out[1]["title"] == "Foundation"


def test_markdown_fenced():
    raw = '```json\n{"books": [{"title": "Dune", "author": "Frank Herbert", "spine_position": 1, "confidence": "high"}]}\n```'
    out = _parse_shelf_response(raw)
    assert len(out) == 1
    assert out[0]["title"] == "Dune"


def test_leading_prose_stripped():
    raw = 'Here are the books I see:\n{"books": [{"title": "Dune", "author": "Frank Herbert", "spine_position": 1, "confidence": "high"}]}'
    out = _parse_shelf_response(raw)
    assert len(out) == 1


def test_trailing_prose_stripped():
    raw = '{"books": [{"title": "Dune", "author": "Frank Herbert", "spine_position": 1, "confidence": "high"}]}\n\nLet me know if you want details.'
    out = _parse_shelf_response(raw)
    assert len(out) == 1


def test_empty_books_array():
    raw = '{"books": []}'
    assert _parse_shelf_response(raw) == []


def test_garbage_returns_empty():
    raw = "absolutely not JSON, just words"
    assert _parse_shelf_response(raw) == []


def test_book_missing_title_dropped():
    raw = '{"books": [{"author": "X", "spine_position": 1, "confidence": "high"}, {"title": "Dune", "author": "F. Herbert", "spine_position": 2, "confidence": "high"}]}'
    out = _parse_shelf_response(raw)
    assert len(out) == 1
    assert out[0]["title"] == "Dune"


def test_bogus_confidence_coerced_to_medium():
    raw = '{"books": [{"title": "Dune", "author": "Frank Herbert", "spine_position": 1, "confidence": "extremely-sure"}]}'
    out = _parse_shelf_response(raw)
    assert out[0]["confidence"] == "medium"


def test_missing_spine_position_uses_index():
    raw = '{"books": [{"title": "A", "author": ""}, {"title": "B", "author": ""}]}'
    out = _parse_shelf_response(raw)
    assert out[0]["spine_position"] == 1
    assert out[1]["spine_position"] == 2


def test_single_book_not_wrapped():
    """Some Ollama models return the inner book object directly. We accept it."""
    raw = '{"title": "Dune", "author": "Frank Herbert", "spine_position": 1, "confidence": "high"}'
    out = _parse_shelf_response(raw)
    assert len(out) == 1
    assert out[0]["title"] == "Dune"


def test_empty_title_dropped_after_strip():
    raw = '{"books": [{"title": "   ", "author": "X", "spine_position": 1, "confidence": "high"}]}'
    out = _parse_shelf_response(raw)
    assert out == []


def test_out_of_order_spine_position_sorted():
    raw = '{"books": [{"title": "B", "author": "", "spine_position": 5, "confidence": "high"}, {"title": "A", "author": "", "spine_position": 2, "confidence": "high"}]}'
    out = _parse_shelf_response(raw)
    assert [b["title"] for b in out] == ["A", "B"]
    assert [b["spine_position"] for b in out] == [2, 5]


def test_author_defaults_to_empty_string():
    raw = '{"books": [{"title": "Dune", "spine_position": 1, "confidence": "high"}]}'
    out = _parse_shelf_response(raw)
    assert out[0]["author"] == ""
