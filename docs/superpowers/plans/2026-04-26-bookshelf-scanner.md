# Bookshelf Scanner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the "Scan Bookshelf" feature defined in `docs/superpowers/specs/2026-04-26-bookshelf-scanner-design.md` end-to-end — photo upload → multimodal vision LLM (Ollama-first) → fuzzy-match via `unified_metadata` → confirmation grid → async bulk-add via `safe_import_manager`. TDD on the parser and ranker logic; mocked HTTP/service tests for the orchestrator; smoke tests for routes.

**Architecture:** New `ShelfScanService` orchestrator class composes the existing `AIService` (extended with one new method) + `fetch_unified_by_title` + `simplified_book_service.create_standalone_book` + `safe_import_manager`. New blueprint `shelf_scan_bp` mounted at `/books/scan` with 4 endpoints. Three new templates plus minor edits to `add_book.html` and `import_books_progress.html`. No new schema, no migrations, reuses every existing primitive that fits.

**Tech Stack:** Python 3.13, Flask, KuzuDB (via existing services), Jinja2, Bootstrap 5, vanilla JS, Pillow (existing), `requests` (existing). Vision LLM via Ollama (default) or OpenAI Vision (opt-in via env). Pytest with mocked HTTP and an in-memory Kuzu fixture (ported from the recommendations branch).

**File map:**

| Path | Purpose | Status |
| --- | --- | --- |
| `app/services/shelf_scan_service.py` | Orchestrator: parser, exceptions, scan store, rate limiter, preprocess, enrichment, public methods | NEW |
| `app/services/ai_service.py` | Add `extract_books_from_shelf_image()` method | MODIFY |
| `app/routes/shelf_scan_routes.py` | Blueprint with upload / confirm / progress / discard / health | NEW |
| `prompts/shelf_scan.mustache` | LLM prompt template (loaded by AIService extension) | NEW |
| `app/templates/shelf_scan_upload.html` | Drag-and-drop upload page | NEW |
| `app/templates/shelf_scan_confirm.html` | Pre-enriched confirmation grid | NEW |
| `tests/conftest.py` | Pytest in-memory Kuzu fixture (ported from recs branch) | NEW |
| `tests/_kuzu_seed.py` | Graph seed helper (ported from recs branch) | NEW |
| `tests/test_shelf_scan_parser.py` | ~12 parser unit tests | NEW |
| `tests/test_shelf_scan_service.py` | Service-level tests against the Kuzu fixture | NEW |
| `tests/test_shelf_scan_routes.py` | Flask test-client smoke tests | NEW |
| `app/services/__init__.py` | Add `shelf_scan_service` lazy singleton | MODIFY |
| `app/routes/__init__.py` | Register `shelf_scan_bp` in `register_blueprints` | MODIFY |
| `app/templates/add_book.html` | Add "Scan Shelf" card to Quick Add Options | MODIFY |
| `app/templates/import_books_progress.html` | Banner when `job.source == 'shelf_scan'` | MODIFY |

**Branch:** All work goes on `claude/bookshelf-scanner` (already pushed). Each task ends with a `git commit`.

---

## Task 1: Pytest in-memory Kuzu fixture

**Files:**
- Create: `tests/_kuzu_seed.py`
- Create: `tests/conftest.py`

The recommendations branch built this same fixture. We port it here so the bookshelf-scanner branch is independently testable without depending on recs being merged first. If recs merges into main first, these files will already exist and git will see the new commit's content as identical (no conflict).

- [ ] **Step 1: Create the seed helper**

```python
# tests/_kuzu_seed.py
"""Graph seed helpers for service-level tests.

Builds a small consistent fixture: 4 users (alice, bob, carol, newbie),
5 authors, 4 series, 6 categories, 13 books, plus reading history that
exercises every signal.

Intentionally NOT named ``test_*.py`` so pytest doesn't try to collect it.
"""
from __future__ import annotations
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List


def _ts(days_ago: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


def seed_graph(conn) -> Dict[str, str]:
    """Populate a fresh KuzuDB connection with deterministic test data.

    Returns a dict mapping semantic names (user_alice, book_dune, ...) to
    UUIDs so tests can reference seeded entities without hard-coding ids.
    """
    ids: Dict[str, str] = {}

    def new_id(name: str) -> str:
        ids[name] = str(uuid.uuid4())
        return ids[name]

    # Lazily create HAS_PERSONAL_METADATA — production app does this on
    # first write; we do it up-front so the seed can write to the rel.
    try:
        conn.execute(
            "CREATE REL TABLE HAS_PERSONAL_METADATA(FROM User TO Book, "
            "personal_notes STRING, start_date TIMESTAMP, finish_date TIMESTAMP, "
            "personal_custom_fields STRING, created_at TIMESTAMP, updated_at TIMESTAMP)",
            {},
        )
    except Exception:
        pass  # Already exists from a prior session

    for handle in ("alice", "bob", "carol", "newbie"):
        uid = new_id(f"user_{handle}")
        conn.execute(
            "CREATE (:User {id: $id, username: $u, email: $e, "
            "share_library: false, share_current_reading: true, "
            "share_reading_activity: true, is_admin: false, "
            "created_at: $ts, updated_at: $ts})",
            {"id": uid, "u": handle, "e": f"{handle}@example.com", "ts": _ts(0)},
        )

    author_specs = [
        ("Frank Herbert", "herbert"),
        ("Isaac Asimov", "asimov"),
        ("Ursula K. Le Guin", "le_guin"),
        ("Brandon Sanderson", "sanderson"),
        ("Andy Weir", "weir"),
    ]
    for name, slug in author_specs:
        pid = new_id(f"author_{slug}")
        conn.execute(
            "CREATE (:Person {id: $id, name: $n, normalized_name: $nn})",
            {"id": pid, "n": name, "nn": name.lower()},
        )

    for sname in ("Dune", "Foundation", "Earthsea", "Stormlight Archive"):
        sid = new_id(f"series_{sname.lower().replace(' ', '_')}")
        conn.execute(
            "CREATE (:Series {id: $id, name: $n, normalized_name: $nn})",
            {"id": sid, "n": sname, "nn": sname.lower()},
        )

    for cname in ("Science Fiction", "Fantasy", "Space Opera",
                  "Hard Science Fiction", "Epic Fantasy", "Classic"):
        cid = new_id(f"cat_{cname.lower().replace(' ', '_')}")
        conn.execute(
            "CREATE (:Category {id: $id, name: $n, normalized_name: $nn})",
            {"id": cid, "n": cname, "nn": cname.lower()},
        )

    book_specs: List = [
        ("dune", "Dune", "en", "herbert", "dune", 1, ["sci_fi", "space_opera"]),
        ("dune2", "Dune Messiah", "en", "herbert", "dune", 2, ["sci_fi", "space_opera"]),
        ("dune3", "Children of Dune", "en", "herbert", "dune", 3, ["sci_fi"]),
        ("foundation", "Foundation", "en", "asimov", "foundation", 1, ["sci_fi", "classic"]),
        ("foundation2", "Foundation and Empire", "en", "asimov", "foundation", 2, ["sci_fi", "classic"]),
        ("foundation3", "Second Foundation", "en", "asimov", "foundation", 3, ["sci_fi", "classic"]),
        ("earthsea", "A Wizard of Earthsea", "en", "le_guin", "earthsea", 1, ["fantasy"]),
        ("earthsea2", "The Tombs of Atuan", "en", "le_guin", "earthsea", 2, ["fantasy"]),
        ("storm1", "The Way of Kings", "en", "sanderson", "stormlight_archive", 1, ["fantasy", "epic_fantasy"]),
        ("storm2", "Words of Radiance", "en", "sanderson", "stormlight_archive", 2, ["fantasy", "epic_fantasy"]),
        ("hail_mary", "Project Hail Mary", "en", "weir", None, None, ["sci_fi", "hard_science_fiction"]),
        ("martian", "The Martian", "en", "weir", None, None, ["sci_fi", "hard_science_fiction"]),
        ("dispossessed", "The Dispossessed", "en", "le_guin", None, None, ["sci_fi"]),
    ]
    for slug, title, lang, author_slug, series_slug, vol, cats in book_specs:
        bid = new_id(f"book_{slug}")
        conn.execute(
            "CREATE (:Book {id: $id, title: $t, normalized_title: $nt, "
            "language: $l, created_at: $ts, updated_at: $ts})",
            {"id": bid, "t": title, "nt": title.lower(), "l": lang, "ts": _ts(0)},
        )
        conn.execute(
            "MATCH (p:Person {id: $pid}), (b:Book {id: $bid}) "
            "CREATE (p)-[:AUTHORED {role: 'authored', order_index: 0}]->(b)",
            {"pid": ids[f"author_{author_slug}"], "bid": bid},
        )
        if series_slug:
            conn.execute(
                "MATCH (b:Book {id: $bid}), (s:Series {id: $sid}) "
                "CREATE (b)-[:PART_OF_SERIES {volume_number: $vol}]->(s)",
                {"bid": bid, "sid": ids[f"series_{series_slug}"], "vol": vol},
            )
        for cat in cats:
            conn.execute(
                "MATCH (b:Book {id: $bid}), (c:Category {id: $cid}) "
                "CREATE (b)-[:CATEGORIZED_AS {created_at: $ts}]->(c)",
                {"bid": bid, "cid": ids[f"cat_{cat}"], "ts": _ts(0)},
            )

    # Reading history — alice/bob/carol have finishes; newbie does not.
    finish_plan = {
        "alice": [("dune", 90), ("dune2", 60), ("dune3", 30), ("foundation", 20)],
        "bob":   [("dune", 95), ("dune2", 70), ("foundation", 40), ("foundation2", 25),
                  ("storm1", 200), ("storm2", 100)],
        "carol": [("dune", 80), ("foundation", 50), ("hail_mary", 15), ("martian", 5)],
    }
    for user_handle, finishes in finish_plan.items():
        for slug, days_ago in finishes:
            conn.execute(
                "MATCH (u:User {id: $uid}), (b:Book {id: $bid}) "
                "CREATE (u)-[:HAS_PERSONAL_METADATA {"
                "finish_date: $fd, created_at: $ts, updated_at: $ts}]->(b)",
                {
                    "uid": ids[f"user_{user_handle}"],
                    "bid": ids[f"book_{slug}"],
                    "fd": _ts(days_ago),
                    "ts": _ts(days_ago),
                },
            )

    return ids
```

- [ ] **Step 2: Create the conftest**

```python
# tests/conftest.py
"""Shared pytest fixtures for graph-touching tests.

Spins up an isolated KuzuDB per test session in a tempdir, applies the
production schema lazily, and seeds a small deterministic graph.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from typing import Iterator, Tuple

import pytest

# Pytest puts conftest's directory on sys.path automatically; the seed file
# sits next to this conftest and is imported as a sibling module (no
# `tests.` prefix because the tests/ dir has no __init__.py).
from _kuzu_seed import seed_graph


@pytest.fixture(scope="session")
def kuzu_tempdir() -> Iterator[str]:
    tmp = tempfile.mkdtemp(prefix="bibliotheca_kuzu_test_")
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="session")
def kuzu_seeded(kuzu_tempdir: str) -> Iterator[Tuple[object, dict]]:
    """Session-scoped seeded KuzuDB connection.

    Returns (connection, named_ids).
    """
    os.environ["KUZU_DB_PATH"] = os.path.join(kuzu_tempdir, "kuzu")
    os.environ["DATA_DIR"] = kuzu_tempdir

    # Reset and reload the singleton manager so it picks up the test paths.
    from app.utils.safe_kuzu_manager import reset_safe_kuzu_manager, get_safe_kuzu_manager
    reset_safe_kuzu_manager()
    mgr = get_safe_kuzu_manager()
    with mgr.get_connection(operation="test_seed") as conn:
        ids = seed_graph(conn)
        yield conn, ids
```

- [ ] **Step 3: Smoke-test the fixture**

Run:
```bash
SECRET_KEY=test python3 -c "
import os, tempfile
tmp = tempfile.mkdtemp()
os.environ['KUZU_DB_PATH'] = os.path.join(tmp, 'kuzu')
os.environ['DATA_DIR'] = tmp
from app.utils.safe_kuzu_manager import reset_safe_kuzu_manager, get_safe_kuzu_manager
reset_safe_kuzu_manager()
import sys; sys.path.insert(0, 'tests')
from _kuzu_seed import seed_graph
with get_safe_kuzu_manager().get_connection(operation='probe') as conn:
    ids = seed_graph(conn)
    print('users:', sum(1 for k in ids if k.startswith('user_')))
    print('books:', sum(1 for k in ids if k.startswith('book_')))
"
```

Expected output:
```
users: 4
books: 13
```

- [ ] **Step 4: Commit**

```bash
git add -f tests/_kuzu_seed.py tests/conftest.py
git commit -m "test(scan): in-memory Kuzu fixture for service-level tests"
```

---

## Task 2: Parser + custom exceptions (TDD)

**Files:**
- Create: `app/services/shelf_scan_service.py` (skeleton with parser + exceptions)
- Create: `tests/test_shelf_scan_parser.py`

The parser is the most fragile piece. TDD against 12 realistic LLM output shapes.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_shelf_scan_parser.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `SECRET_KEY=test python3 -m pytest tests/test_shelf_scan_parser.py -v`

Expected: ImportError on `_parse_shelf_response` (module/function doesn't exist).

- [ ] **Step 3: Implement the module skeleton + parser**

```python
# app/services/shelf_scan_service.py
"""Bookshelf-scanner orchestrator service.

Coordinates: image preprocessing → vision LLM → fuzzy metadata match →
confirmation grid → async bulk-add. See
docs/superpowers/specs/2026-04-26-bookshelf-scanner-design.md for the
design rationale.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ---- Custom exceptions ---------------------------------------------------

class ShelfScanError(Exception):
    """Base for shelf-scan failures the route layer should surface to the user."""


class ShelfScanLLMUnavailable(ShelfScanError):
    """Raised when no AI provider is configured or all providers failed."""


class ShelfScanEmptyResult(ShelfScanError):
    """Raised when the LLM returned 0 readable spines.

    Attribute ``preview_url`` is set to the upload preview path so the
    upload page can re-render with the original photo retained.
    """
    def __init__(self, preview_url: str = ""):
        super().__init__("No readable spines detected.")
        self.preview_url = preview_url


# ---- Parser -------------------------------------------------------------

_VALID_CONFIDENCE = {"high", "medium", "low"}
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _parse_shelf_response(raw: str) -> List[Dict[str, Any]]:
    """Parse a vision-LLM response into a normalised book list.

    Tolerates:
      - Markdown ` ```json ... ``` ` fences (Ollama habit).
      - Leading prose ("Here are the books I see:").
      - Trailing prose after the JSON block.
      - A single book object not wrapped in {"books": [...]}.

    Coerces:
      - Missing spine_position → enumeration index (1-based).
      - Missing/invalid confidence → "medium".
      - Missing author → "".

    Drops:
      - Books with empty title (after .strip()).
      - Anything that isn't a dict at the top level / inside "books".

    Returns books sorted by spine_position. Returns [] on any unrecoverable
    parse failure (the caller treats [] as ShelfScanEmptyResult upstream).
    """
    if not raw or not isinstance(raw, str):
        return []

    text = _FENCE_RE.sub("", raw).strip()
    # Pull out the first balanced-looking JSON object from anywhere in the
    # response; this strips leading/trailing prose without us building a
    # full JSON parser.
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1 or last < first:
        return []
    candidate = text[first:last + 1]

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return []

    if not isinstance(data, dict):
        return []

    # Accept either {"books":[...]} or a single-book dict.
    if "books" in data and isinstance(data["books"], list):
        raw_books = data["books"]
    elif "title" in data:
        raw_books = [data]
    else:
        return []

    books: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_books):
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        author = (item.get("author") or "").strip()
        try:
            spine_position = int(item.get("spine_position"))
        except (TypeError, ValueError):
            spine_position = idx + 1
        confidence = item.get("confidence")
        if confidence not in _VALID_CONFIDENCE:
            confidence = "medium"
        books.append({
            "title": title,
            "author": author,
            "spine_position": spine_position,
            "confidence": confidence,
        })

    books.sort(key=lambda b: b["spine_position"])
    return books
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `SECRET_KEY=test python3 -m pytest tests/test_shelf_scan_parser.py -v`

Expected: 13 tests pass (the 12 listed plus the bonus `test_author_defaults_to_empty_string`).

- [ ] **Step 5: Commit**

```bash
git add -f tests/test_shelf_scan_parser.py
git add app/services/shelf_scan_service.py
git commit -m "feat(scan): parser + custom exceptions (TDD)"
```

---

## Task 3: LLM prompt template

**Files:**
- Create: `prompts/shelf_scan.mustache`

The existing `AIService` looks for prompts under `prompts/` (verified in `_load_prompt_template` with the `book_extraction.mustache` template). Sibling file for shelf scanning.

- [ ] **Step 1: Write the prompt**

```mustache
{{! prompts/shelf_scan.mustache — bookshelf spine extraction prompt }}
You are looking at a photograph of a bookshelf. Identify EVERY book whose
spine is at least partly readable. For each book, extract the title and the
author exactly as printed on the spine.

Rules:
- Return books in left-to-right order as they appear on the shelf.
- If a spine has no readable text, skip it (do not guess).
- If the title or author is partially obscured, return what you can read
  and mark confidence accordingly.
- Do NOT invent books that aren't visible.
- Do NOT include shelves, dividers, decorations, or non-book objects.

Respond with ONLY valid JSON in this exact shape, with no surrounding prose:

{
  "books": [
    {
      "title": "<title as printed>",
      "author": "<author as printed, or empty string if not visible>",
      "spine_position": <integer, 1-based left-to-right>,
      "confidence": "high" | "medium" | "low"
    }
  ]
}

confidence values:
- "high"   — both title and author clearly readable
- "medium" — one of (title|author) is clear, the other partial or guessed
- "low"    — significantly obscured; user should verify carefully
```

- [ ] **Step 2: Sanity-check the file exists at the right path**

Run: `ls prompts/ | grep -E "(book_extraction|shelf_scan)"`

Expected output (both files present):
```
book_extraction.mustache
shelf_scan.mustache
```

- [ ] **Step 3: Commit**

```bash
git add prompts/shelf_scan.mustache
git commit -m "feat(scan): bookshelf-spine LLM prompt template"
```

---

## Task 4: AIService.extract_books_from_shelf_image

**Files:**
- Modify: `app/services/ai_service.py`
- Create: `tests/test_shelf_scan_aiservice.py`

Reuses the existing OpenAI/Ollama provider plumbing — only adds the new prompt loader, response parser, and a method that dispatches the multi-book call. Tests mock the HTTP layer via `responses`-style monkeypatching.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_shelf_scan_aiservice.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `SECRET_KEY=test python3 -m pytest tests/test_shelf_scan_aiservice.py -v`

Expected: AttributeError on `extract_books_from_shelf_image` and `is_configured`.

- [ ] **Step 3: Implement the AIService extension**

Find the line `def extract_book_info_from_image(self, image_data: bytes, filename: str)` in `app/services/ai_service.py` (around line 35). Add these methods to the `AIService` class — after `extract_book_info_from_image` and before `_load_prompt_template`. Note: re-use the existing `_extract_with_openai` / `_extract_with_ollama` provider methods by adding optional `prompt_template_name` and `parser` parameters; if that's too invasive at this stage, write standalone provider calls in the new method (the code below uses standalone calls to keep the diff minimal).

Insert the following block in `app/services/ai_service.py` immediately before `def _load_prompt_template`:

```python
    def is_configured(self) -> bool:
        """True when at least one AI provider has the credentials we need.

        Used by the shelf-scan upload page to decide whether to disable
        the submit button. Mirrors the provider-selection logic in
        extract_book_info_from_image.
        """
        primary = (self.provider or "openai").lower()
        if primary == "openai":
            if self.config.get("OPENAI_API_KEY"):
                return True
        if primary == "ollama":
            if self.config.get("OLLAMA_BASE_URL"):
                return True
        # Fallback considered configured when the OTHER provider is set.
        if self.config.get("AI_FALLBACK_ENABLED", "true").lower() == "true":
            if self.config.get("OPENAI_API_KEY") or self.config.get("OLLAMA_BASE_URL"):
                return True
        return False

    def extract_books_from_shelf_image(self, image_data: bytes) -> list:
        """Extract a list of {title, author, spine_position, confidence}
        from a bookshelf photo.

        Uses the same provider-selection + fallback dance as
        extract_book_info_from_image but with a different prompt and a
        multi-book parser. Returns [] if nothing usable came back.
        """
        # Local import keeps the module's top-level imports light and
        # avoids a circular import if shelf_scan_service ever imports
        # ai_service eagerly.
        from .shelf_scan_service import _parse_shelf_response

        prompt = self._load_shelf_scan_prompt()

        primary = (self.provider or "openai").lower()
        secondary = "ollama" if primary == "openai" else "openai"
        providers_to_try = [primary]

        fallback_enabled = self.config.get("AI_FALLBACK_ENABLED", "true").lower() == "true"
        other_configured = (
            (secondary == "openai" and bool(self.config.get("OPENAI_API_KEY")))
            or (secondary == "ollama" and bool(self.config.get("OLLAMA_BASE_URL", "http://localhost:11434")))
        )
        if fallback_enabled and other_configured:
            providers_to_try.append(secondary)

        last_error = None
        for prov in providers_to_try:
            try:
                if prov == "openai":
                    raw = self._call_openai_vision(image_data, prompt)
                elif prov == "ollama":
                    raw = self._call_ollama_vision(image_data, prompt)
                else:
                    continue
                if raw is None:
                    continue
                parsed = _parse_shelf_response(raw)
                if parsed:
                    return parsed
                # Empty parse — try the next provider before giving up.
            except Exception as e:
                last_error = e
                try:
                    current_app.logger.warning(f"Shelf-scan provider {prov} failed: {e}")
                except Exception:
                    pass

        if last_error:
            try:
                current_app.logger.error(
                    f"Shelf scan failed after trying providers {providers_to_try}: {last_error}"
                )
            except Exception:
                pass
        return []

    def _load_shelf_scan_prompt(self) -> str:
        path = os.path.join(os.path.dirname(current_app.root_path), "prompts", "shelf_scan.mustache")
        if not os.path.exists(path):
            # Inline fallback so missing-file deployments still work; matches
            # the prompt content in the spec.
            return (
                "You are looking at a photograph of a bookshelf. Identify EVERY book "
                "whose spine is at least partly readable. Return ONLY valid JSON: "
                '{"books":[{"title":"...","author":"...","spine_position":1,"confidence":"high"}]}.'
            )
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def _call_openai_vision(self, image_data: bytes, prompt: str) -> str | None:
        api_key = self.config.get("OPENAI_API_KEY")
        if not api_key:
            return None
        base_url = self.config.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        model = self.config.get("OPENAI_MODEL", "gpt-4o-mini")
        b64 = base64.b64encode(image_data).decode("ascii")
        body = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
            "max_tokens": self.max_tokens,
            "temperature": float(self.config.get("AI_TEMPERATURE", "0.1")),
            # response_format guarantees JSON for shelf-scan parsing.
            "response_format": {"type": "json_object"},
        }
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            return None
        return resp.json()["choices"][0]["message"]["content"]

    def _call_ollama_vision(self, image_data: bytes, prompt: str) -> str | None:
        base_url = self.config.get("OLLAMA_BASE_URL", "http://localhost:11434")
        model = self.config.get("OLLAMA_MODEL", "llama3.2-vision")
        b64 = base64.b64encode(image_data).decode("ascii")
        body = {
            "model": model,
            "stream": False,
            "messages": [{
                "role": "user",
                "content": prompt + "\n\nReturn ONLY the JSON object, no markdown, no commentary.",
                "images": [b64],
            }],
            "options": {
                "temperature": float(self.config.get("AI_TEMPERATURE", "0.1")),
                "num_predict": self.max_tokens,
            },
        }
        resp = requests.post(f"{base_url}/api/chat", json=body, timeout=self.timeout)
        if resp.status_code != 200:
            return None
        return resp.json()["message"]["content"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `SECRET_KEY=test python3 -m pytest tests/test_shelf_scan_aiservice.py -v`

Expected: 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/ai_service.py
git add -f tests/test_shelf_scan_aiservice.py
git commit -m "feat(scan): AIService.extract_books_from_shelf_image"
```

---

## Task 5: Image preprocessing on the service

**Files:**
- Modify: `app/services/shelf_scan_service.py`
- Create: `tests/test_shelf_scan_service.py`

Adds `ShelfScanService` class with the `_preprocess` method only — service is built up incrementally over the next several tasks.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_shelf_scan_service.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `SECRET_KEY=test python3 -m pytest tests/test_shelf_scan_service.py -v`

Expected: ImportError on `ShelfScanService`.

- [ ] **Step 3: Implement the service skeleton + _preprocess**

Append to `app/services/shelf_scan_service.py`:

```python
import io
import os
from typing import Tuple

from PIL import Image, UnidentifiedImageError


# Long-edge cap for resize. 2048 keeps spine recognition accurate while
# cutting cloud-LLM payload ~10x for typical phone photos.
MAX_LONG_EDGE = 2048
# JPEG quality for the resized preview / LLM input.
JPEG_QUALITY = 85
# Allowed input formats per PIL.
_ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


class ShelfScanService:
    """Orchestrator for the bookshelf-scanner feature.

    Wires AIService (vision LLM) → unified_metadata (fuzzy match) →
    simplified_book_service (bulk add) into a single user-facing flow.
    """

    def __init__(self):
        # Scan store, rate limiter, in-flight tracker — populated in later tasks.
        # Declared here so the `service` fixture can construct one without args.
        pass

    # ---- Public-ish helpers (exposed for tests) -------------------------

    def _uploads_dir(self) -> str:
        """Resolve the uploads/scans directory.

        Order of precedence (matches existing image_processing.get_covers_dir
        pattern):
          1. /app/data/uploads/scans (Docker)
          2. {DATA_DIR}/uploads/scans
          3. {repo_root}/data/uploads/scans
        Creates the directory if missing.
        """
        from flask import current_app
        candidate = "/app/data/uploads/scans"
        if not os.path.isdir(candidate):
            data_dir = None
            try:
                data_dir = current_app.config.get("DATA_DIR")
            except Exception:
                data_dir = None
            if data_dir:
                candidate = os.path.join(data_dir, "uploads", "scans")
            else:
                # repo_root/data/uploads/scans
                root = os.path.dirname(current_app.root_path) if hasattr(current_app, "root_path") else os.getcwd()
                candidate = os.path.join(root, "data", "uploads", "scans")
        os.makedirs(candidate, exist_ok=True)
        return candidate

    def _preprocess(
        self,
        image_bytes: bytes,
        original_filename: str,
        scan_id: str,
    ) -> Tuple[bytes, str]:
        """Validate, resize, and persist a preview of the uploaded image.

        Returns ``(resized_jpeg_bytes, preview_url)`` where preview_url is
        the relative URL the confirmation page can render via the existing
        /uploads/<...> static handler.

        Raises ``ValueError`` for unsupported / corrupt image inputs.
        """
        try:
            with Image.open(io.BytesIO(image_bytes)) as probe:
                probe.verify()
        except (UnidentifiedImageError, Exception) as e:
            raise ValueError(f"Invalid image data: {e}") from e

        # verify() consumed the file pointer; re-open for actual decode.
        try:
            img = Image.open(io.BytesIO(image_bytes))
        except UnidentifiedImageError as e:
            raise ValueError("Invalid image data") from e

        if img.format not in _ALLOWED_FORMATS:
            raise ValueError(f"Unsupported image format: {img.format!r}")

        # Resize so the longer edge is <= MAX_LONG_EDGE.
        if max(img.size) > MAX_LONG_EDGE:
            ratio = MAX_LONG_EDGE / max(img.size)
            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            img = img.resize(new_size, Image.LANCZOS)

        # Always emit JPEG (smaller, lower bandwidth to LLMs).
        if img.mode != "RGB":
            img = img.convert("RGB")
        out_buf = io.BytesIO()
        img.save(out_buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        resized_bytes = out_buf.getvalue()

        out_path = os.path.join(self._uploads_dir(), f"{scan_id}.jpg")
        with open(out_path, "wb") as f:
            f.write(resized_bytes)

        preview_url = f"/uploads/scans/{scan_id}.jpg"
        return resized_bytes, preview_url
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `SECRET_KEY=test python3 -m pytest tests/test_shelf_scan_service.py -v`

Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/shelf_scan_service.py
git add -f tests/test_shelf_scan_service.py
git commit -m "feat(scan): image preprocessing (validate, resize, preview)"
```

---

## Task 6: Scan store + rate limiter

**Files:**
- Modify: `app/services/shelf_scan_service.py`
- Modify: `tests/test_shelf_scan_service.py`

In-memory state with proper locking. Single-worker app makes this safe.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_shelf_scan_service.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `SECRET_KEY=test python3 -m pytest tests/test_shelf_scan_service.py -v -k "scan_store or rate_limiter or in_flight"`

Expected: ImportError on `ShelfScanRateLimited` and AttributeError on the new methods.

- [ ] **Step 3: Implement scan store + rate limiter**

Append to `app/services/shelf_scan_service.py`:

```python
import threading
import time

# TTL for the scan store entries (1 hour).
SCAN_STORE_TTL_SECONDS = 3600
# Max scans per user per 24h. Override via env: SHELF_SCAN_DAILY_LIMIT_PER_USER.
DAILY_SCAN_LIMIT_PER_USER = int(os.environ.get("SHELF_SCAN_DAILY_LIMIT_PER_USER", "30"))
# How long an in-flight marker is valid (90s) before we forget about it.
IN_FLIGHT_TTL_SECONDS = 90
# Window for the rate limiter (24h).
RATE_LIMIT_WINDOW_SECONDS = 86400


class ShelfScanRateLimited(ShelfScanError):
    """Raised when a user has exceeded DAILY_SCAN_LIMIT_PER_USER."""


class ShelfScanInProgress(ShelfScanError):
    """Raised when a user already has a scan in flight."""
```

Then modify `ShelfScanService.__init__` to initialise the data structures. Replace the existing `__init__` body with:

```python
    def __init__(self):
        self._scan_store: Dict[str, Dict[str, Any]] = {}
        self._scan_store_lock = threading.RLock()
        self._rate_limit: Dict[str, list] = {}            # user_id -> [ts, ts, ...] (last 24h only)
        self._rate_limit_lock = threading.RLock()
        self._in_flight: Dict[str, float] = {}            # user_id -> start_ts
        self._in_flight_lock = threading.RLock()
```

Also append the new instance methods to the class body:

```python
    # ---- Scan store ---------------------------------------------------

    def _save_scan(self, scan_id: str, user_id: str, candidates: list,
                   preview_url: str = "", summary: dict | None = None) -> None:
        now = time.time()
        with self._scan_store_lock:
            # Sweep expired entries opportunistically.
            for k in list(self._scan_store.keys()):
                if self._scan_store[k]["expires_at"] < now:
                    self._scan_store.pop(k, None)
            self._scan_store[scan_id] = {
                "user_id": user_id,
                "candidates": candidates,
                "preview_url": preview_url,
                "summary": summary or {},
                "expires_at": now + SCAN_STORE_TTL_SECONDS,
            }

    def get_scan(self, scan_id: str, user_id: str) -> dict | None:
        with self._scan_store_lock:
            entry = self._scan_store.get(scan_id)
            if not entry:
                return None
            if entry["user_id"] != user_id:
                return None
            if entry["expires_at"] < time.time():
                self._scan_store.pop(scan_id, None)
                return None
            return entry

    def discard_scan(self, scan_id: str, user_id: str) -> bool:
        with self._scan_store_lock:
            entry = self._scan_store.get(scan_id)
            if not entry or entry["user_id"] != user_id:
                return False
            self._scan_store.pop(scan_id, None)
        # Remove preview file if present (best-effort).
        try:
            preview_path = os.path.join(self._uploads_dir(), f"{scan_id}.jpg")
            if os.path.exists(preview_path):
                os.unlink(preview_path)
        except Exception:
            logger.exception("shelf_scan: failed to remove preview file %s", scan_id)
        return True

    # ---- Rate limiter --------------------------------------------------

    def _record_scan_for_rate_limit(self, user_id: str) -> None:
        """Record a scan in the rate-limit window, raising if over the cap.

        Imported limits read from the module-level DAILY_SCAN_LIMIT_PER_USER
        so tests can monkeypatch.
        """
        from app.services import shelf_scan_service as _module
        limit = _module.DAILY_SCAN_LIMIT_PER_USER
        now = time.time()
        with self._rate_limit_lock:
            entries = self._rate_limit.get(user_id, [])
            # Drop entries older than the rate-limit window.
            entries = [ts for ts in entries if now - ts < RATE_LIMIT_WINDOW_SECONDS]
            if len(entries) >= limit:
                self._rate_limit[user_id] = entries
                raise ShelfScanRateLimited(
                    f"Daily scan limit ({limit}) reached. Try again later."
                )
            entries.append(now)
            self._rate_limit[user_id] = entries

    # ---- In-flight tracking -------------------------------------------

    def _mark_scan_in_flight(self, user_id: str) -> None:
        now = time.time()
        with self._in_flight_lock:
            existing_ts = self._in_flight.get(user_id)
            if existing_ts is not None and now - existing_ts < IN_FLIGHT_TTL_SECONDS:
                raise ShelfScanInProgress(
                    "A scan is already in progress for this user. Please wait."
                )
            self._in_flight[user_id] = now

    def _clear_scan_in_flight(self, user_id: str) -> None:
        with self._in_flight_lock:
            self._in_flight.pop(user_id, None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `SECRET_KEY=test python3 -m pytest tests/test_shelf_scan_service.py -v`

Expected: 12 tests pass total (4 from Task 5 + 8 from this task).

- [ ] **Step 5: Commit**

```bash
git add app/services/shelf_scan_service.py tests/test_shelf_scan_service.py
git commit -m "feat(scan): scan store + rate limiter + in-flight tracker"
```

---

## Task 7: Enrichment helper (parallel unified_metadata lookups)

**Files:**
- Modify: `app/services/shelf_scan_service.py`
- Modify: `tests/test_shelf_scan_service.py`

For each detection, fuzzy-match against `fetch_unified_by_title`. Bounded concurrency to avoid hammering Google Books / OpenLibrary.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_shelf_scan_service.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `SECRET_KEY=test python3 -m pytest tests/test_shelf_scan_service.py -v -k "enrich"`

Expected: AttributeError on `_enrich_one`.

- [ ] **Step 3: Implement `_enrich_one`**

Append to `app/services/shelf_scan_service.py`:

```python
from concurrent.futures import ThreadPoolExecutor

from app.utils.unified_metadata import fetch_unified_by_title


# Title-search results 0 → best_match, 1..MAX_ALTERNATIVES → alternatives.
MAX_ALTERNATIVES = 4
# Bounded concurrency for parallel enrichment.
ENRICHMENT_WORKERS = 4


def _project_match(m: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce a unified_metadata result to the fields the confirmation card uses."""
    return {
        "title": m.get("title", ""),
        "authors": m.get("authors") or [],
        "isbn13": m.get("isbn13"),
        "isbn10": m.get("isbn10"),
        "cover_url": m.get("cover_url"),
        "published_date": m.get("published_date"),
        "page_count": m.get("page_count"),
        "language": m.get("language"),
        "description": m.get("description"),
        "similarity_score": m.get("similarity_score"),
    }


def _shelf_extend_class():
    """Module-level helper to make pytest happy with class-method patching."""
    pass
```

Then add the `_enrich_one` method to the `ShelfScanService` class body:

```python
    def _enrich_one(self, detection: Dict[str, Any], detection_id: str) -> Dict[str, Any]:
        """Fuzzy-match one detection against unified_metadata, return a candidate dict.

        Failure modes (network error, empty result) collapse into
        matched=False — never raise from here.
        """
        title = detection.get("title", "")
        author = detection.get("author") or None
        try:
            results = fetch_unified_by_title(title, max_results=MAX_ALTERNATIVES + 1, author=author)
        except Exception:
            logger.exception("shelf_scan: enrichment lookup failed for %r", title)
            results = []

        best_match = _project_match(results[0]) if results else None
        alternatives = [_project_match(r) for r in results[1:1 + MAX_ALTERNATIVES]]
        matched = best_match is not None
        default_selected = bool(matched and detection.get("confidence") == "high")

        return {
            "detection_id": detection_id,
            "spine_position": int(detection.get("spine_position") or 0),
            "confidence": detection.get("confidence", "medium"),
            "detected": {
                "title": title,
                "author": detection.get("author", ""),
            },
            "matched": matched,
            "best_match": best_match,
            "alternatives": alternatives,
            "default_selected": default_selected,
        }

    def _enrich_many(self, detections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Run _enrich_one across all detections in parallel."""
        if not detections:
            return []
        items = [(d, f"det_{i + 1:03d}") for i, d in enumerate(detections)]
        with ThreadPoolExecutor(max_workers=ENRICHMENT_WORKERS) as ex:
            return list(ex.map(lambda pair: self._enrich_one(pair[0], pair[1]), items))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `SECRET_KEY=test python3 -m pytest tests/test_shelf_scan_service.py -v`

Expected: 16 tests pass (12 prior + 4 new).

- [ ] **Step 5: Commit**

```bash
git add app/services/shelf_scan_service.py tests/test_shelf_scan_service.py
git commit -m "feat(scan): per-detection metadata enrichment + parallel pool"
```

---

## Task 8: scan_image_and_enrich_sync — the orchestrator

**Files:**
- Modify: `app/services/shelf_scan_service.py`
- Modify: `tests/test_shelf_scan_service.py`

The public entry point. Wires preprocess → AIService → enrichment → owned-filter → store. Tests mock the LLM and metadata calls; pre-existing books in the Kuzu fixture exercise the owned-filter.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_shelf_scan_service.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `SECRET_KEY=test python3 -m pytest tests/test_shelf_scan_service.py -v -k "test_scan_"`

Expected: AttributeError on `scan_image_and_enrich_sync` and `_user_owned_book_titles`.

- [ ] **Step 3: Implement the orchestrator**

Append to `app/services/shelf_scan_service.py`:

```python
import time
import uuid

from app.services.ai_service import AIService


def _load_ai_config() -> Dict[str, str]:
    """Build the config dict AIService expects from env / app config."""
    # Mirror the env-driven config used by other AIService callers
    # (admin.load_ai_config does similar work). Kept local so we don't need
    # to import admin (which has heavier deps).
    return {
        "AI_PROVIDER": os.environ.get("AI_PROVIDER", "ollama"),
        "OLLAMA_BASE_URL": os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        "OLLAMA_MODEL": os.environ.get("OLLAMA_MODEL", "llama3.2-vision"),
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
        "OPENAI_MODEL": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        "OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "AI_FALLBACK_ENABLED": os.environ.get("AI_FALLBACK_ENABLED", "true"),
        "AI_TIMEOUT": os.environ.get("AI_TIMEOUT", "60"),
        "AI_MAX_TOKENS": os.environ.get("AI_MAX_TOKENS", "1500"),
        "AI_TEMPERATURE": os.environ.get("AI_TEMPERATURE", "0.1"),
    }
```

Then add these methods to `ShelfScanService`:

```python
    def _user_owned_book_titles(self, user_id: str) -> set:
        """Return the set of {isbn13|isbn10} the user already owns.

        We use ISBNs because they're the only stable join key — comparing
        titles is fragile across editions.
        """
        try:
            from app.infrastructure.kuzu_graph import safe_execute_kuzu_query
            result = safe_execute_kuzu_query(
                "MATCH (u:User {id: $uid})-[:HAS_PERSONAL_METADATA]->(b:Book) "
                "RETURN b.isbn13 AS isbn13, b.isbn10 AS isbn10",
                {"uid": user_id},
                user_id=user_id,
                operation="shelf_scan_owned",
            )
            owned: set = set()
            if result is None:
                return owned
            has_next = getattr(result, "has_next", None)
            get_next = getattr(result, "get_next", None)
            if callable(has_next) and callable(get_next):
                while result.has_next():
                    row = result.get_next()
                    if row[0]:
                        owned.add(str(row[0]))
                    if row[1]:
                        owned.add(str(row[1]))
            return owned
        except Exception:
            logger.exception("shelf_scan: failed to fetch owned ISBNs")
            return set()

    def scan_image_and_enrich_sync(
        self,
        image_bytes: bytes,
        user_id: str,
        original_filename: str,
    ) -> Dict[str, Any]:
        """End-to-end synchronous scan + enrichment. Returns a dict with
        ``{scan_id, candidates, summary, preview_url}``.

        Raises:
            ShelfScanRateLimited — over the daily cap
            ShelfScanInProgress — concurrent scan in flight
            ShelfScanLLMUnavailable — no provider or all providers failed
            ShelfScanEmptyResult — LLM returned 0 spines
        """
        t_total = time.perf_counter()

        # 1. Pre-flight: rate limit + in-flight gate.
        self._record_scan_for_rate_limit(user_id)
        self._mark_scan_in_flight(user_id)
        try:
            # 2. AI provider must be configured.
            ai = AIService(_load_ai_config())
            if not ai.is_configured():
                raise ShelfScanLLMUnavailable("No AI provider configured")

            # 3. Allocate a scan_id up front so the preview path is stable.
            scan_id = uuid.uuid4().hex

            # 4. Preprocess (validate + resize + write preview file).
            t_pre = time.perf_counter()
            resized_bytes, preview_url = self._preprocess(image_bytes, original_filename, scan_id)
            preprocess_ms = int((time.perf_counter() - t_pre) * 1000)

            # 5. Vision LLM call.
            t_llm = time.perf_counter()
            detections = ai.extract_books_from_shelf_image(resized_bytes)
            llm_ms = int((time.perf_counter() - t_llm) * 1000)

            if not detections:
                raise ShelfScanEmptyResult(preview_url=preview_url)

            # 6. Parallel enrichment.
            t_en = time.perf_counter()
            enriched = self._enrich_many(detections)
            enrich_ms = int((time.perf_counter() - t_en) * 1000)

            # 7. Filter already-owned books.
            owned_isbns = self._user_owned_book_titles(user_id)
            already_owned_count = 0
            kept: List[Dict[str, Any]] = []
            for c in enriched:
                if c.get("matched"):
                    bm = c.get("best_match") or {}
                    isbn13 = bm.get("isbn13")
                    isbn10 = bm.get("isbn10")
                    if (isbn13 and isbn13 in owned_isbns) or (isbn10 and isbn10 in owned_isbns):
                        already_owned_count += 1
                        continue
                kept.append(c)

            summary = {
                "detected": len(detections),
                "matched": sum(1 for c in kept if c.get("matched")),
                "already_owned": already_owned_count,
                "unmatched": sum(1 for c in kept if not c.get("matched")),
            }

            # 8. Persist for /confirm.
            self._save_scan(scan_id, user_id, kept, preview_url=preview_url, summary=summary)

            total_ms = int((time.perf_counter() - t_total) * 1000)
            logger.info(
                "[shelf_scan] user=%s provider=%s detected=%s matched=%s "
                "already_owned=%s unmatched=%s preprocess_ms=%s llm_ms=%s "
                "enrich_ms=%s total_ms=%s",
                user_id, ai.provider, summary["detected"], summary["matched"],
                summary["already_owned"], summary["unmatched"],
                preprocess_ms, llm_ms, enrich_ms, total_ms,
            )

            return {
                "scan_id": scan_id,
                "candidates": kept,
                "summary": summary,
                "preview_url": preview_url,
            }
        finally:
            self._clear_scan_in_flight(user_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `SECRET_KEY=test python3 -m pytest tests/test_shelf_scan_service.py -v`

Expected: 21 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/shelf_scan_service.py tests/test_shelf_scan_service.py
git commit -m "feat(scan): scan_image_and_enrich_sync orchestrator"
```

---

## Task 9: start_bulk_add_async + bulk-add worker

**Files:**
- Modify: `app/services/shelf_scan_service.py`
- Modify: `tests/test_shelf_scan_service.py`

Async path that creates books and links them to the user as `library_only`. Reuses `safe_import_manager` for progress tracking.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_shelf_scan_service.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `SECRET_KEY=test python3 -m pytest tests/test_shelf_scan_service.py -v -k "bulk_add"`

Expected: AttributeError on `start_bulk_add_async` and `_create_and_link_book`.

- [ ] **Step 3: Implement the bulk-add worker**

Append to `app/services/shelf_scan_service.py`:

```python
import threading as _threading
import uuid as _uuid
from datetime import datetime, timezone

from app.utils.safe_import_manager import (
    safe_create_import_job,
    safe_get_import_job,
    safe_update_import_job,
)


def _resolve_chosen_metadata(candidate: Dict[str, Any], overrides: Dict[str, int]) -> Dict[str, Any] | None:
    """Pick the metadata dict the user committed to for this candidate.

    overrides[detection_id] = N → use alternatives[N]; otherwise use best_match.
    Returns None if the candidate has no usable metadata (e.g. unmatched).
    """
    det_id = candidate.get("detection_id")
    n = overrides.get(det_id)
    if n is not None:
        try:
            return candidate["alternatives"][int(n)]
        except (KeyError, IndexError, ValueError):
            pass
    return candidate.get("best_match")
```

Then add the bulk-add methods to `ShelfScanService`:

```python
    def _create_and_link_book(self, user_id: str, candidate_metadata: Dict[str, Any]) -> str | None:
        """Create the Book node + HAS_PERSONAL_METADATA edge with reading_status=library_only.

        Returns the new book id (or None if creation failed). Callers
        translate None → counted as an error.

        Note: simplified_book_service lives at app.simplified_book_service
        (NOT app.services.simplified_book_service) and exposes both async
        and sync create methods. We use the *_sync* variant because this
        method runs inside a worker thread, not an event loop.
        """
        from app.simplified_book_service import SimplifiedBookService, SimplifiedBook
        from app.services.personal_metadata_service import PersonalMetadataService

        book = SimplifiedBook(
            title=candidate_metadata.get("title", ""),
            authors=", ".join(candidate_metadata.get("authors") or []),
            isbn=candidate_metadata.get("isbn13") or candidate_metadata.get("isbn10") or "",
            isbn_13=candidate_metadata.get("isbn13") or "",
            isbn_10=candidate_metadata.get("isbn10") or "",
            description=candidate_metadata.get("description") or "",
            cover_url=candidate_metadata.get("cover_url") or "",
            language=candidate_metadata.get("language") or "en",
            page_count=candidate_metadata.get("page_count"),
            published_date=candidate_metadata.get("published_date") or "",
        )
        simplified_service = SimplifiedBookService()
        book_id = simplified_service.create_standalone_book_sync(book)
        if not book_id:
            return None

        # Link to the user's library with library_only status. The
        # personal_metadata_service custom_updates dict is the right place
        # for non-column fields like reading_status.
        try:
            PersonalMetadataService().update_personal_metadata(
                user_id=user_id,
                book_id=book_id,
                custom_updates={"reading_status": "library_only"},
            )
        except Exception:
            logger.exception("shelf_scan: failed to link %s to user %s", book_id, user_id)
            # Book exists in graph but not linked — surface as an error
            # so the user knows they need to add it manually. Counts as
            # a failure for this candidate.
            return None
        return book_id

    def start_bulk_add_async(
        self,
        user_id: str,
        scan_id: str,
        picked: List[str],
        overrides: Dict[str, int],
    ) -> str:
        """Kick off the background bulk-add. Returns the task_id immediately.

        The route layer hands the task_id to the existing import progress
        page (/import/progress/<task_id>) which polls safe_import_manager.
        """
        scan = self.get_scan(scan_id, user_id)
        if not scan:
            raise ShelfScanError("scan_id not found, expired, or not owned by user")

        task_id = _uuid.uuid4().hex
        job_data = {
            "task_id": task_id,
            "user_id": user_id,
            "status": "pending",
            "processed": 0,
            "success": 0,
            "errors": 0,
            "skipped": 0,
            "total": len(picked),
            "current_book": None,
            "error_messages": [],
            "processed_books": [],
            "source": "shelf_scan",
            "scan_id": scan_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        safe_create_import_job(user_id, task_id, job_data)

        thread = _threading.Thread(
            target=self._bulk_add_worker,
            args=(user_id, task_id, scan_id, picked, overrides),
            daemon=True,
            name=f"shelf-scan-bulk-{task_id[:8]}",
        )
        thread.start()
        return task_id

    def _bulk_add_worker(
        self,
        user_id: str,
        task_id: str,
        scan_id: str,
        picked: List[str],
        overrides: Dict[str, int],
    ) -> None:
        """Background worker: iterate picked candidates, create books, log progress."""
        scan = self.get_scan(scan_id, user_id)
        if not scan:
            safe_update_import_job(user_id, task_id, {"status": "failed",
                                                      "error_messages": [{"error": "scan expired"}]})
            return

        # Index candidates by detection_id for O(1) lookup.
        cand_by_id = {c["detection_id"]: c for c in scan["candidates"]}
        successes = 0
        errors: List[Dict[str, str]] = []

        safe_update_import_job(user_id, task_id, {"status": "running"})

        for det_id in picked:
            candidate = cand_by_id.get(det_id)
            if not candidate:
                errors.append({"detection_id": det_id, "error": "detection_id not in scan"})
                safe_update_import_job(user_id, task_id, {
                    "processed": successes + len(errors),
                    "errors": len(errors),
                    "error_messages": errors,
                })
                continue
            metadata = _resolve_chosen_metadata(candidate, overrides)
            if not metadata:
                errors.append({"detection_id": det_id, "error": "no usable metadata"})
                safe_update_import_job(user_id, task_id, {
                    "processed": successes + len(errors),
                    "errors": len(errors),
                    "error_messages": errors,
                })
                continue
            try:
                book_id = self._create_and_link_book(user_id, metadata)
                if not book_id:
                    raise RuntimeError("create_standalone_book returned None")
                successes += 1
                safe_update_import_job(user_id, task_id, {
                    "processed": successes + len(errors),
                    "success": successes,
                    "current_book": metadata.get("title", ""),
                })
            except Exception as e:
                logger.exception("shelf_scan: bulk-add failed for %s", det_id)
                errors.append({"detection_id": det_id, "error": str(e)})
                safe_update_import_job(user_id, task_id, {
                    "processed": successes + len(errors),
                    "errors": len(errors),
                    "error_messages": errors,
                })

        safe_update_import_job(user_id, task_id, {"status": "completed"})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `SECRET_KEY=test python3 -m pytest tests/test_shelf_scan_service.py -v`

Expected: 24 tests pass (21 prior + 3 bulk-add).

- [ ] **Step 5: Commit**

```bash
git add app/services/shelf_scan_service.py tests/test_shelf_scan_service.py
git commit -m "feat(scan): start_bulk_add_async + worker"
```

---

## Task 10: Lazy singleton wiring in app/services/__init__.py

**Files:**
- Modify: `app/services/__init__.py`

Mirror the `book_service` / `recommendation_service` lazy-singleton pattern.

- [ ] **Step 1: Read the current __init__.py to find the right insertion points**

Run: `grep -n "_get_recommendation_service\|recommendation_service = _LazyService\|_recommendation_service = None" app/services/__init__.py`

Expected: at least one match if the recs branch's lazy singleton has been merged. If zero matches (audit-only branch — likely your case here), use the `_get_book_service` block as the template instead.

- [ ] **Step 2: Add the lazy getter after the existing `_get_*_service` block**

In `app/services/__init__.py`, near the existing `_get_book_service` definition (around line 52–58 in the audit branch), append:

```python
    _shelf_scan_service = None

    def _get_shelf_scan_service():
        global _shelf_scan_service
        if _shelf_scan_service is None:
            _run_migration_once()
            from .shelf_scan_service import ShelfScanService
            _shelf_scan_service = ShelfScanService()
        return _shelf_scan_service
```

- [ ] **Step 3: Add the lazy instance alongside `book_service`**

Find the line `book_service = _LazyService(_get_book_service)` and add immediately after:

```python
    shelf_scan_service = _LazyService(_get_shelf_scan_service)
```

- [ ] **Step 4: Add reset hook**

Inside `reset_all_services()`, alongside the other resets, add:

```python
        global _shelf_scan_service
        global shelf_scan_service
        _shelf_scan_service = None
        if hasattr(shelf_scan_service, "_service"):
            shelf_scan_service._service = None  # type: ignore[attr-defined]
```

And after the wrapper recreation block, add:

```python
        shelf_scan_service = _LazyService(_get_shelf_scan_service)
```

- [ ] **Step 5: Add to __all__**

Find the `__all__` list and add `'shelf_scan_service'`.

- [ ] **Step 6: Verify the singleton imports**

Run: `SECRET_KEY=test python3 -c "from app.services import shelf_scan_service; print(type(shelf_scan_service))"`

Expected: `<class 'app.services._LazyService'>` (or similar; not an ImportError).

- [ ] **Step 7: Commit**

```bash
git add app/services/__init__.py
git commit -m "feat(scan): lazy singleton wiring for shelf_scan_service"
```

---

## Task 11: Blueprint with 5 routes + smoke tests

**Files:**
- Create: `app/routes/shelf_scan_routes.py`
- Modify: `app/routes/__init__.py`
- Create: `tests/test_shelf_scan_routes.py`

Five endpoints: `GET /` (upload page), `POST /upload`, `POST /confirm`, `GET /progress/<task_id>`, `POST /<scan_id>/discard`. Health check route lives separately at `/admin/scan/health` and is added in this same task.

- [ ] **Step 1: Create the blueprint**

```python
# app/routes/shelf_scan_routes.py
"""Bookshelf scanner blueprint.

All endpoints require login. The upload route is synchronous (~30-80s for
the LLM call); the confirm route kicks off a background bulk-add via the
existing safe_import_manager and returns a task_id. The progress page is
the existing /import/progress/<task_id> page (we just feed the same job
shape into it).
"""
from __future__ import annotations

import logging

from flask import (
    Blueprint, current_app, flash, jsonify, redirect, render_template, request,
    url_for,
)
from flask_login import current_user, login_required

from app.services import shelf_scan_service
from app.services.shelf_scan_service import (
    ShelfScanLLMUnavailable, ShelfScanEmptyResult, ShelfScanRateLimited,
    ShelfScanInProgress, ShelfScanError,
)

logger = logging.getLogger(__name__)

shelf_scan_bp = Blueprint("shelf_scan", __name__, url_prefix="/books/scan")


def _ai_provider_label() -> dict:
    """Build the user-facing AI-provider notice for the upload page."""
    import os
    provider = os.environ.get("AI_PROVIDER", "ollama").lower()
    ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    ollama_model = os.environ.get("OLLAMA_MODEL", "llama3.2-vision")
    openai_model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_ollama = bool(ollama_url)
    if provider == "openai" and has_openai:
        return {"provider": "openai", "model": openai_model, "configured": True,
                "label": f"Using OpenAI Vision ({openai_model}) — ~$0.02 per scan."}
    if provider == "ollama" and has_ollama:
        return {"provider": "ollama", "model": ollama_model, "configured": True,
                "label": f"Using local Ollama at {ollama_url} (model {ollama_model})."}
    if has_openai:
        return {"provider": "openai", "model": openai_model, "configured": True,
                "label": f"Using OpenAI Vision ({openai_model}) — ~$0.02 per scan."}
    if has_ollama:
        return {"provider": "ollama", "model": ollama_model, "configured": True,
                "label": f"Using local Ollama at {ollama_url} (model {ollama_model})."}
    return {"provider": None, "model": None, "configured": False,
            "label": "No AI provider configured. Set AI_PROVIDER, OLLAMA_BASE_URL, or OPENAI_API_KEY."}


@shelf_scan_bp.route("/", methods=["GET"])
@login_required
def upload_page():
    return render_template("shelf_scan_upload.html",
                           ai=_ai_provider_label(),
                           error=None,
                           preview_url=None)


@shelf_scan_bp.route("/upload", methods=["POST"])
@login_required
def upload():
    file = request.files.get("shelf_image")
    if not file or file.filename == "":
        flash("Please choose an image to upload.", "warning")
        return redirect(url_for("shelf_scan.upload_page"))
    image_bytes = file.read()
    user_id = str(current_user.id)
    try:
        result = shelf_scan_service.scan_image_and_enrich_sync(
            image_bytes=image_bytes,
            user_id=user_id,
            original_filename=file.filename,
        )
    except ShelfScanLLMUnavailable:
        return render_template("shelf_scan_upload.html",
                               ai=_ai_provider_label(),
                               error="Vision model is unavailable. Check that Ollama is running, "
                                     "or set AI_PROVIDER=openai with a key.",
                               preview_url=None)
    except ShelfScanEmptyResult as e:
        return render_template("shelf_scan_upload.html",
                               ai=_ai_provider_label(),
                               error="We couldn't read any spines in that photo. "
                                     "Try a clearer, well-lit photo.",
                               preview_url=e.preview_url)
    except ShelfScanRateLimited as e:
        return render_template("shelf_scan_upload.html",
                               ai=_ai_provider_label(),
                               error=str(e),
                               preview_url=None), 429
    except ShelfScanInProgress as e:
        return render_template("shelf_scan_upload.html",
                               ai=_ai_provider_label(),
                               error=str(e),
                               preview_url=None), 409
    except ValueError as e:
        return render_template("shelf_scan_upload.html",
                               ai=_ai_provider_label(),
                               error=f"Image validation failed: {e}",
                               preview_url=None), 400
    except Exception:
        logger.exception("shelf_scan upload failed")
        return render_template("shelf_scan_upload.html",
                               ai=_ai_provider_label(),
                               error="An unexpected error occurred. Please try again.",
                               preview_url=None), 500

    return render_template("shelf_scan_confirm.html", **result)


@shelf_scan_bp.route("/confirm", methods=["POST"])
@login_required
def confirm():
    import json as _json
    scan_id = request.form.get("scan_id", "").strip()
    picked = request.form.getlist("detection_id")
    try:
        overrides = _json.loads(request.form.get("overrides") or "{}")
    except _json.JSONDecodeError:
        overrides = {}
    user_id = str(current_user.id)
    if not scan_id:
        return jsonify({"status": "error", "message": "scan_id required"}), 400
    if not picked:
        return jsonify({"status": "error", "message": "no books selected"}), 400
    if shelf_scan_service.get_scan(scan_id, user_id) is None:
        return jsonify({"status": "error", "message": "Scan expired or not found"}), 410
    try:
        task_id = shelf_scan_service.start_bulk_add_async(
            user_id=user_id,
            scan_id=scan_id,
            picked=picked,
            overrides=overrides,
        )
    except ShelfScanError as e:
        return jsonify({"status": "error", "message": str(e)}), 410
    return jsonify({"status": "success", "task_id": task_id})


@shelf_scan_bp.route("/progress/<task_id>", methods=["GET"])
@login_required
def progress(task_id: str):
    from app.utils.safe_import_manager import safe_get_import_job
    user_id = str(current_user.id)
    job = safe_get_import_job(user_id, task_id)
    if not job:
        return jsonify({"status": "error", "message": "task not found"}), 404
    return jsonify({"status": "success", "job": job})


@shelf_scan_bp.route("/<scan_id>/discard", methods=["POST"])
@login_required
def discard(scan_id: str):
    user_id = str(current_user.id)
    ok = shelf_scan_service.discard_scan(scan_id, user_id)
    if not ok:
        return jsonify({"status": "error", "message": "scan not found"}), 404
    return jsonify({"status": "success"})


# ---- Admin health check ------------------------------------------------

shelf_scan_admin_bp = Blueprint("shelf_scan_admin", __name__, url_prefix="/admin/scan")


@shelf_scan_admin_bp.route("/health", methods=["GET"])
@login_required
def health():
    """Probe the configured AI provider with a 1x1 dummy image. Cached 5 min."""
    if not getattr(current_user, "is_admin", False):
        return jsonify({"status": "error", "message": "admin required"}), 403
    import io as _io, base64 as _b64, time as _time
    from PIL import Image as _Image
    from app.services.ai_service import AIService
    from app.services.shelf_scan_service import _load_ai_config

    # 5-min in-memory cache, keyed by provider.
    cache = getattr(health, "_cache", {})
    health._cache = cache  # type: ignore[attr-defined]
    cfg = _load_ai_config()
    provider = cfg.get("AI_PROVIDER", "ollama")
    cached = cache.get(provider)
    if cached and (_time.time() - cached["ts"] < 300):
        return jsonify(cached["payload"])

    # Build a tiny PNG (1x1) and ask the model to extract — we don't care
    # about the result, only that the call returns 200.
    img = _Image.new("RGB", (1, 1), color=(255, 255, 255))
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    image_bytes = buf.getvalue()

    ai = AIService(cfg)
    t0 = _time.time()
    try:
        ai.extract_books_from_shelf_image(image_bytes)
        ok = True
        err = None
    except Exception as e:
        ok = False
        err = str(e)
    latency_ms = int((_time.time() - t0) * 1000)
    payload = {
        "provider": provider,
        "model": cfg.get("OLLAMA_MODEL") if provider == "ollama" else cfg.get("OPENAI_MODEL"),
        "ok": ok,
        "latency_ms": latency_ms,
        "error": err,
    }
    cache[provider] = {"ts": _time.time(), "payload": payload}
    return jsonify(payload)
```

- [ ] **Step 2: Register both blueprints**

In `app/routes/__init__.py`, add to the imports near the top:

```python
from .shelf_scan_routes import shelf_scan_bp, shelf_scan_admin_bp
```

Inside `register_blueprints()`, after the existing reading_logs / books / etc. registrations, add:

```python
    app.register_blueprint(shelf_scan_bp)
    app.register_blueprint(shelf_scan_admin_bp)
```

Add `'shelf_scan_bp'` and `'shelf_scan_admin_bp'` to the module's `__all__` list at the bottom.

- [ ] **Step 3: Write the smoke tests**

```python
# tests/test_shelf_scan_routes.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `SECRET_KEY=test python3 -m pytest tests/test_shelf_scan_routes.py -v`

Expected: 10 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/routes/shelf_scan_routes.py app/routes/__init__.py
git add -f tests/test_shelf_scan_routes.py
git commit -m "feat(scan): blueprint with upload/confirm/progress/discard + admin health"
```

---

## Task 12: Upload page template

**Files:**
- Create: `app/templates/shelf_scan_upload.html`

- [ ] **Step 1: Write the page**

```jinja
{% extends "base.html" %}

{% block title %}Scan Bookshelf - MyBibliotheca{% endblock %}

{% block content %}
<style>
  .scan-upload-zone {
    border: 2px dashed var(--border-soft, rgba(0,0,0,.15));
    border-radius: 10px;
    padding: 2rem;
    text-align: center;
    background: var(--surface-secondary, #fafafa);
    cursor: pointer;
    transition: border-color 120ms ease, background 120ms ease;
  }
  .scan-upload-zone.dragover {
    border-color: var(--bs-primary);
    background: rgba(13, 110, 253, .05);
  }
  .scan-preview-thumb {
    max-width: 320px; max-height: 240px;
    border-radius: 8px; margin: 1rem auto; display: block;
    border: 1px solid var(--border-soft, rgba(0,0,0,.1));
  }
</style>

<div class="container">
  <h1 class="mb-3">Scan a bookshelf</h1>
  <p class="text-muted">Take or upload a clear, well-lit photo of your bookshelf — we'll identify multiple books at once.</p>

  {% if error %}
  <div class="alert alert-warning d-flex justify-content-between align-items-start" role="alert">
    <div>{{ error }}</div>
  </div>
  {% endif %}

  <div class="card mb-3">
    <div class="card-body">
      <form method="POST" action="{{ url_for('shelf_scan.upload') }}" enctype="multipart/form-data" id="shelf-scan-form">
        <div class="scan-upload-zone" id="upload-zone">
          <div id="upload-zone-content">
            <i class="bi bi-camera fs-1"></i>
            <p class="mb-1">Drop a JPEG/PNG/WebP here, or</p>
            <label class="btn btn-outline-primary mb-0">
              Choose a file
              <input type="file" name="shelf_image" accept="image/jpeg,image/png,image/webp"
                     class="visually-hidden" id="shelf-image-input">
            </label>
          </div>
          <img id="shelf-image-preview" class="scan-preview-thumb"
               {% if preview_url %}src="{{ preview_url }}"{% endif %}
               style="{% if not preview_url %}display:none;{% endif %}">
        </div>

        <div class="alert alert-info mt-3 mb-3 small d-flex align-items-center" role="status">
          <i class="bi bi-info-circle me-2"></i>
          <div>{{ ai.label }}</div>
        </div>

        <button type="submit" class="btn btn-primary w-100" id="shelf-scan-submit"
                {% if not ai.configured %}disabled{% endif %}>
          <span class="default-label">Scan shelf</span>
          <span class="loading-label d-none">
            <span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>
            Scanning… 30–80 seconds
          </span>
        </button>
      </form>
    </div>
  </div>
</div>

<script>
(function(){
  const zone = document.getElementById('upload-zone');
  const input = document.getElementById('shelf-image-input');
  const preview = document.getElementById('shelf-image-preview');
  const form = document.getElementById('shelf-scan-form');
  const submit = document.getElementById('shelf-scan-submit');
  const defaultLabel = submit.querySelector('.default-label');
  const loadingLabel = submit.querySelector('.loading-label');
  const zoneContent = document.getElementById('upload-zone-content');

  function showPreview(file) {
    if (!file) return;
    const url = URL.createObjectURL(file);
    preview.src = url;
    preview.style.display = '';
    zoneContent.style.display = 'none';
  }

  if (zone && input) {
    zone.addEventListener('click', function(e){
      // Don't double-fire when clicking the inner label.
      if (e.target.tagName !== 'INPUT' && e.target.tagName !== 'LABEL') input.click();
    });
    ['dragenter','dragover'].forEach(evt => zone.addEventListener(evt, e => {
      e.preventDefault(); e.stopPropagation(); zone.classList.add('dragover');
    }));
    ['dragleave','drop'].forEach(evt => zone.addEventListener(evt, e => {
      e.preventDefault(); e.stopPropagation(); zone.classList.remove('dragover');
    }));
    zone.addEventListener('drop', e => {
      const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) {
        const dt = new DataTransfer();
        dt.items.add(file);
        input.files = dt.files;
        showPreview(file);
      }
    });
    input.addEventListener('change', () => showPreview(input.files[0]));
  }

  form.addEventListener('submit', () => {
    submit.disabled = true;
    defaultLabel.classList.add('d-none');
    loadingLabel.classList.remove('d-none');
    if (input) input.disabled = true;
  });
})();
</script>
{% endblock %}
```

- [ ] **Step 2: Verify Jinja parses**

Run:
```bash
SECRET_KEY=test python3 -c "
from jinja2 import Environment, FileSystemLoader, ChoiceLoader, DictLoader
loader = ChoiceLoader([
    DictLoader({'base.html': '{% block title %}{% endblock %}{% block content %}{% endblock %}{% block scripts %}{% endblock %}'}),
    FileSystemLoader('app/templates'),
])
env = Environment(loader=loader)
env.get_template('shelf_scan_upload.html')
print('ok')
"
```

Expected: `ok`.

- [ ] **Step 3: Run the route tests to confirm rendering still passes**

Run: `SECRET_KEY=test python3 -m pytest tests/test_shelf_scan_routes.py -v -k "upload_page_renders"`

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add app/templates/shelf_scan_upload.html
git commit -m "feat(scan): upload page template"
```

---

## Task 13: Confirmation page template

**Files:**
- Create: `app/templates/shelf_scan_confirm.html`

- [ ] **Step 1: Write the page**

```jinja
{% extends "base.html" %}

{% block title %}Confirm Scan Results - MyBibliotheca{% endblock %}

{% block content %}
<style>
  .scan-summary { display: grid; gap: .5rem 1rem; grid-template-columns: max-content 1fr; align-items: baseline; }
  .scan-summary dt { font-weight: 600; }
  .scan-summary dd { margin: 0; }
  .scan-card { transition: transform 100ms ease, box-shadow 100ms ease; }
  .scan-card:hover { transform: translateY(-2px); box-shadow: 0 4px 12px rgba(0,0,0,.06); }
  .scan-card .cover-wrap { aspect-ratio: 2/3; overflow: hidden; background: #eee; border-radius: 6px 6px 0 0; }
  .scan-card .cover-wrap img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .scan-card.disabled { opacity: .55; }
  .scan-card .conf-badge { font-size: .7rem; }
  .conf-high { background: #198754; color: #fff; }
  .conf-medium { background: #ffc107; color: #000; }
  .conf-low { background: #dc3545; color: #fff; }
</style>

<div class="container">
  <h1 class="mb-3">Scan results</h1>

  <div class="row mb-3 g-3">
    <div class="col-md-4">
      {% if preview_url %}
      <a href="{{ preview_url }}" target="_blank" rel="noopener">
        <img src="{{ preview_url }}" alt="Bookshelf preview"
             class="img-fluid rounded border">
      </a>
      {% endif %}
    </div>
    <div class="col-md-8">
      <dl class="scan-summary">
        <dt>Detected</dt><dd>{{ summary.detected }}</dd>
        <dt>Matched in our database</dt><dd>{{ summary.matched }}</dd>
        <dt>Already in your library</dt><dd>{{ summary.already_owned }}</dd>
        <dt>Unmatched</dt><dd>{{ summary.unmatched }}</dd>
      </dl>
    </div>
  </div>

  {% if candidates %}
  <div class="d-flex gap-2 flex-wrap mb-3">
    <button type="button" class="btn btn-outline-secondary btn-sm" id="select-all-btn">Select all</button>
    <button type="button" class="btn btn-outline-secondary btn-sm" id="select-none-btn">Select none</button>
    <button type="button" class="btn btn-outline-secondary btn-sm" id="select-high-btn">Only high confidence</button>
  </div>

  <form id="confirm-form">
    <input type="hidden" name="scan_id" value="{{ scan_id }}">
    <input type="hidden" name="overrides" id="overrides-input" value="{}">

    <div class="row g-3" id="card-grid">
      {% for c in candidates %}
        {% set bm = c.best_match %}
        {% set is_matched = c.matched %}
        {% set conf = c.confidence %}
        <div class="col-12 col-sm-6 col-lg-3 scan-card-col" data-detection-id="{{ c.detection_id }}">
          <div class="card scan-card h-100 {% if not is_matched %}disabled{% endif %}">
            <div class="cover-wrap">
              {% if is_matched and bm and bm.cover_url %}
                <img src="{{ bm.cover_url }}" alt="" class="cover-img"
                     onerror="this.src='/static/bookshelf.png'">
              {% else %}
                <img src="/static/bookshelf.png" alt="">
              {% endif %}
            </div>
            <div class="card-body p-2">
              <div class="d-flex justify-content-between mb-1">
                <small class="text-muted">#{{ c.spine_position }}</small>
                <span class="badge conf-{{ conf }} conf-badge">{{ conf }}</span>
              </div>
              {% if is_matched and bm %}
                <div class="fw-semibold small card-title-text">{{ bm.title }}</div>
                <div class="text-muted small card-author-text">{{ bm.authors|join(', ') if bm.authors else '' }}</div>
              {% else %}
                <div class="fw-semibold small">{{ c.detected.title }}</div>
                <div class="text-muted small">{{ c.detected.author }}</div>
                <div class="alert alert-warning p-1 mt-2 small mb-0">
                  Couldn't find in metadata.
                  <a target="_blank" rel="noopener"
                     href="{{ url_for('main.add_book') }}?title={{ c.detected.title|urlencode }}">Search manually</a>
                </div>
              {% endif %}

              <div class="form-check mt-2">
                <input class="form-check-input scan-pick" type="checkbox" name="detection_id"
                       value="{{ c.detection_id }}"
                       id="pick-{{ c.detection_id }}"
                       data-confidence="{{ conf }}"
                       {% if c.default_selected %}checked{% endif %}
                       {% if not is_matched %}disabled{% endif %}>
                <label class="form-check-label small" for="pick-{{ c.detection_id }}">Add to library</label>
              </div>

              {% if is_matched and c.alternatives %}
              <details class="mt-2 small">
                <summary>Other editions ({{ c.alternatives|length }})</summary>
                <select class="form-select form-select-sm mt-1 alt-select"
                        data-detection-id="{{ c.detection_id }}">
                  <option value="-1" selected>{{ bm.title }} (top match)</option>
                  {% for alt in c.alternatives %}
                  <option value="{{ loop.index0 }}">{{ alt.title }} — {{ alt.authors|join(', ') }}</option>
                  {% endfor %}
                </select>
              </details>
              {% endif %}
            </div>
          </div>
        </div>
      {% endfor %}
    </div>

    <div class="d-flex justify-content-between align-items-center my-4">
      <button type="button" class="btn btn-outline-danger" id="discard-btn">Discard scan</button>
      <button type="submit" class="btn btn-primary" id="submit-btn">
        Add <span id="submit-count">0</span> selected books →
      </button>
    </div>
  </form>
  {% else %}
  <div class="alert alert-info">
    No candidates to confirm. Try a different photo.
    <a href="{{ url_for('shelf_scan.upload_page') }}" class="alert-link">Scan another shelf</a>.
  </div>
  {% endif %}
</div>

<script>
(function(){
  const grid = document.getElementById('card-grid');
  if (!grid) return;
  const submitBtn = document.getElementById('submit-btn');
  const countEl = document.getElementById('submit-count');
  const overridesInput = document.getElementById('overrides-input');
  const form = document.getElementById('confirm-form');
  const scanId = form.querySelector('[name="scan_id"]').value;

  const overrides = {};

  function refreshSubmitState() {
    const checked = grid.querySelectorAll('.scan-pick:checked:not(:disabled)').length;
    countEl.textContent = checked;
    submitBtn.disabled = checked === 0;
  }

  grid.addEventListener('change', function(e){
    if (e.target.classList.contains('scan-pick')) refreshSubmitState();
    if (e.target.classList.contains('alt-select')) {
      const detId = e.target.dataset.detectionId;
      const idx = parseInt(e.target.value, 10);
      if (Number.isInteger(idx) && idx >= 0) {
        overrides[detId] = idx;
      } else {
        delete overrides[detId];
      }
      overridesInput.value = JSON.stringify(overrides);
      // Update displayed title/author of the card to match the selected alt.
      const opt = e.target.selectedOptions[0];
      const card = e.target.closest('.scan-card');
      const title = card.querySelector('.card-title-text');
      if (title && opt && opt.textContent) {
        const split = opt.textContent.split(' — ');
        title.textContent = split[0];
        const author = card.querySelector('.card-author-text');
        if (author) author.textContent = split.slice(1).join(' — ');
      }
    }
  });

  document.getElementById('select-all-btn').addEventListener('click', function(){
    grid.querySelectorAll('.scan-pick:not(:disabled)').forEach(cb => cb.checked = true);
    refreshSubmitState();
  });
  document.getElementById('select-none-btn').addEventListener('click', function(){
    grid.querySelectorAll('.scan-pick').forEach(cb => cb.checked = false);
    refreshSubmitState();
  });
  document.getElementById('select-high-btn').addEventListener('click', function(){
    grid.querySelectorAll('.scan-pick:not(:disabled)').forEach(cb => {
      cb.checked = (cb.dataset.confidence === 'high');
    });
    refreshSubmitState();
  });

  document.getElementById('discard-btn').addEventListener('click', function(){
    if (!confirm('Discard this scan? Anything you haven’t added will be lost.')) return;
    fetch('/books/scan/' + encodeURIComponent(scanId) + '/discard', {
      method: 'POST', credentials: 'same-origin'
    }).then(() => { window.location = '/books/scan/'; });
  });

  form.addEventListener('submit', function(e){
    e.preventDefault();
    submitBtn.disabled = true;
    submitBtn.textContent = 'Starting…';
    const fd = new FormData(form);
    fetch('/books/scan/confirm', {
      method: 'POST', body: fd, credentials: 'same-origin',
      headers: { 'Accept': 'application/json' },
    })
      .then(r => r.json())
      .then(payload => {
        if (payload.status === 'success') {
          window.location = '/import/progress/' + encodeURIComponent(payload.task_id);
        } else {
          alert(payload.message || 'Something went wrong');
          submitBtn.disabled = false;
          refreshSubmitState();
        }
      })
      .catch(err => {
        console.warn(err);
        alert('Network error. Please try again.');
        submitBtn.disabled = false;
        refreshSubmitState();
      });
  });

  refreshSubmitState();
})();
</script>
{% endblock %}
```

- [ ] **Step 2: Verify Jinja parses**

Run:
```bash
SECRET_KEY=test python3 -c "
from jinja2 import Environment, FileSystemLoader, ChoiceLoader, DictLoader
loader = ChoiceLoader([
    DictLoader({'base.html': '{% block title %}{% endblock %}{% block content %}{% endblock %}{% block scripts %}{% endblock %}'}),
    FileSystemLoader('app/templates'),
])
env = Environment(loader=loader)
env.get_template('shelf_scan_confirm.html')
print('ok')
"
```

Expected: `ok`.

- [ ] **Step 3: Re-run all route smoke tests**

Run: `SECRET_KEY=test python3 -m pytest tests/test_shelf_scan_routes.py -v`

Expected: 10 tests pass.

- [ ] **Step 4: Commit**

```bash
git add app/templates/shelf_scan_confirm.html
git commit -m "feat(scan): confirmation page template"
```

---

## Task 14: "Scan Shelf" card on Add Book page

**Files:**
- Modify: `app/templates/add_book.html`

The existing Quick Add Options sidebar has two cards (ISBN Lookup, File Upload). Add a third sibling.

- [ ] **Step 1: Find the insertion point**

Run: `grep -n "ISBN Lookup\|File Upload\|Quick Add" app/templates/add_book.html | head -10`

Take note of the line where the existing "File Upload" card ends — that's where the new card goes.

- [ ] **Step 2: Insert the new card**

Add this block immediately after the existing File Upload card's closing tag (typically `</div>` followed by another card or end of the sidebar `<div>`):

```jinja
          <div class="card mb-3">
            <div class="card-body">
              <h6 class="card-title">
                <i class="bi bi-camera me-1"></i>📷 Scan Shelf
                <span class="text-muted small ms-1" data-bs-toggle="tooltip"
                      title="Take a photo of your bookshelf and identify multiple books at once">
                  <i class="bi bi-info-circle"></i>
                </span>
              </h6>
              <p class="text-muted small mb-2">
                Upload a photo of your bookshelf and let AI identify multiple books at once.
              </p>
              <a href="{{ url_for('shelf_scan.upload_page') }}" class="btn btn-outline-primary w-100">
                <i class="bi bi-arrow-right me-1"></i>Open Scanner
              </a>
            </div>
          </div>
```

- [ ] **Step 3: Verify Jinja parses**

Run:
```bash
SECRET_KEY=test python3 -c "
from jinja2 import Environment, FileSystemLoader, ChoiceLoader, DictLoader
loader = ChoiceLoader([
    DictLoader({'base.html': '{% block title %}{% endblock %}{% block content %}{% endblock %}{% block scripts %}{% endblock %}{% block styles %}{% endblock %}{% block extra_modals %}{% endblock %}'}),
    FileSystemLoader('app/templates'),
])
env = Environment(loader=loader)
env.get_template('add_book.html')
print('ok')
"
```

Expected: `ok`.

- [ ] **Step 4: Run the full test suite**

Run: `SECRET_KEY=test python3 -m pytest tests/ -v 2>&1 | tail -10`

Expected: All shelf-scan tests pass; no regressions in pre-existing tests.

- [ ] **Step 5: Commit**

```bash
git add app/templates/add_book.html
git commit -m "feat(scan): Scan Shelf card on Add Book page"
```

---

## Task 15: Source banner on the import progress page

**Files:**
- Modify: `app/templates/import_books_progress.html`

The CSV-import progress template is what users get redirected to after `/books/scan/confirm`. Add a tiny banner when the job's `source == 'shelf_scan'`.

- [ ] **Step 1: Find where the progress template renders the heading**

Run: `grep -n "{% block content %}\|<h1\|<h2\|book.*source\|'source'" app/templates/import_books_progress.html | head -15`

The banner goes near the top of the content block, above the existing progress heading.

- [ ] **Step 2: Insert the banner**

Just after the line where `{% block content %}` opens (usually within the first 30 lines), insert:

```jinja
{# Source banner — set when the job came from the bookshelf scanner #}
{% if job and job.source == 'shelf_scan' %}
<div class="alert alert-info d-flex align-items-center" role="status">
  <i class="bi bi-camera me-2"></i>
  <div>Importing {{ job.total or 0 }} books from a shelf scan.</div>
</div>
{% endif %}
```

If the template doesn't already pass `job` into the context, the banner is a no-op (the `{% if job and job.source %}` short-circuits). The route serving the progress page already passes `job` — verify by:

Run: `grep -n "render_template.*import_books_progress\|job=" app/routes/import_routes.py | head -5`

Expected: a `render_template('import_books_progress.html', ...)` call that includes a `job=` kwarg or similar. If it doesn't, that's a separate fix (out of scope for this task; the banner just won't render).

- [ ] **Step 3: Verify Jinja parses**

Run:
```bash
SECRET_KEY=test python3 -c "
from jinja2 import Environment, FileSystemLoader, ChoiceLoader, DictLoader
loader = ChoiceLoader([
    DictLoader({'base.html': '{% block title %}{% endblock %}{% block content %}{% endblock %}{% block scripts %}{% endblock %}'}),
    FileSystemLoader('app/templates'),
])
env = Environment(loader=loader)
env.get_template('import_books_progress.html')
print('ok')
"
```

Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add app/templates/import_books_progress.html
git commit -m "feat(scan): banner on import progress page when source=shelf_scan"
```

---

## Task 16: End-to-end manual smoke test

**No new files.** Verify the whole feature in the running dev container.

- [ ] **Step 1: Run the full pytest suite**

```bash
SECRET_KEY=test python3 -m pytest tests/ -v 2>&1 | tail -15
```

Expected: every shelf-scan test passes; no regressions in `test_unified_metadata.py` or any other pre-existing tests.

- [ ] **Step 2: Hot-load all changes into the dev container**

```bash
docker cp app/services/shelf_scan_service.py mybibliotheca-bibliotheca-1:/app/app/services/shelf_scan_service.py
docker cp app/services/ai_service.py mybibliotheca-bibliotheca-1:/app/app/services/ai_service.py
docker cp app/services/__init__.py mybibliotheca-bibliotheca-1:/app/app/services/__init__.py
docker cp app/routes/shelf_scan_routes.py mybibliotheca-bibliotheca-1:/app/app/routes/shelf_scan_routes.py
docker cp app/routes/__init__.py mybibliotheca-bibliotheca-1:/app/app/routes/__init__.py
docker cp app/templates/shelf_scan_upload.html mybibliotheca-bibliotheca-1:/app/app/templates/shelf_scan_upload.html
docker cp app/templates/shelf_scan_confirm.html mybibliotheca-bibliotheca-1:/app/app/templates/shelf_scan_confirm.html
docker cp app/templates/add_book.html mybibliotheca-bibliotheca-1:/app/app/templates/add_book.html
docker cp app/templates/import_books_progress.html mybibliotheca-bibliotheca-1:/app/app/templates/import_books_progress.html
docker cp prompts/shelf_scan.mustache mybibliotheca-bibliotheca-1:/app/prompts/shelf_scan.mustache
docker compose -f docker-compose.dev.yml restart bibliotheca
sleep 6
curl -s -o /dev/null -w "GET / -> %{http_code}\n" http://localhost:5054/
```

Expected: `GET / -> 302` (redirect to login).

- [ ] **Step 3: Verify Ollama is reachable from inside the container**

```bash
docker exec mybibliotheca-bibliotheca-1 sh -c \
  'curl -s http://host.docker.internal:11434/api/tags | head -c 200 || echo "Ollama unreachable"'
```

Expected: a JSON object with `{"models": [...]}` (Ollama is running on the host and reachable). If Ollama is on a different host, set `OLLAMA_BASE_URL` in `docker-compose.dev.yml` accordingly and restart.

If Ollama is reachable but `llama3.2-vision` (or your configured model) is not pulled, run on the host:

```bash
ollama pull llama3.2-vision
```

- [ ] **Step 4: Walk through the surfaces in the browser**

Log in, then check:

- [ ] `/books/add` shows a third "📷 Scan Shelf" card alongside ISBN Lookup and File Upload.
- [ ] `/books/scan/` renders the upload page with the AI-provider notice ("Using local Ollama at …" or "Using OpenAI Vision (~$0.02 per scan)").
- [ ] Drop a real bookshelf JPEG (≥4 books). The scan completes in 30–80s and you land on the confirmation page with cards.
- [ ] The summary panel shows non-zero "Detected" / "Matched" counts.
- [ ] At least one card has cover + title + author populated; high-confidence cards default-checked.
- [ ] The "Other editions" dropdown on a matched card swaps the displayed title when you pick an alternative.
- [ ] Click "Add N selected books →" — the page redirects to `/import/progress/<task_id>` with the "Importing N books from a shelf scan" banner. Progress increments to N successes.
- [ ] After completion, navigate to `/library` and the books are present with `library_only` status.
- [ ] Try an `/admin/scan/health` request as the admin — returns `{provider, model, ok, latency_ms}`.
- [ ] Try `/admin/scan/health` as a non-admin — returns 403.

- [ ] **Step 5: Commit any polish you made during the smoke test**

```bash
git add -u
git commit -m "polish(scan): smoke-test adjustments" || echo "nothing to commit"
```

If nothing changed, no commit is needed.

---

## Done.

The feature is shippable when:

1. All four test files pass: `tests/test_shelf_scan_parser.py`, `tests/test_shelf_scan_aiservice.py`, `tests/test_shelf_scan_service.py`, `tests/test_shelf_scan_routes.py`.
2. Manual walk-through above is green for at least one real bookshelf photo.
3. `python3 -m py_compile` on every changed file is clean.

If anything fails during the smoke test (slow LLM, weird prompt output, layout glitches), file follow-ups — don't paper over them in this implementation.
