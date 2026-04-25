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


# ---- Image preprocessing ------------------------------------------------

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
