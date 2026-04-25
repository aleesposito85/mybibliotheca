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
