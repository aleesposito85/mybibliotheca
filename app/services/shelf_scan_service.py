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
import threading
import time
from typing import Any, Dict, Tuple

from PIL import Image, UnidentifiedImageError


# Long-edge cap for resize. 2048 keeps spine recognition accurate while
# cutting cloud-LLM payload ~10x for typical phone photos.
MAX_LONG_EDGE = 2048
# JPEG quality for the resized preview / LLM input.
JPEG_QUALITY = 85
# Allowed input formats per PIL.
_ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}

from concurrent.futures import ThreadPoolExecutor

from app.utils.unified_metadata import fetch_unified_by_title

# TTL for the scan store entries (1 hour).
SCAN_STORE_TTL_SECONDS = 3600
# Max scans per user per 24h. Override via env: SHELF_SCAN_DAILY_LIMIT_PER_USER.
DAILY_SCAN_LIMIT_PER_USER = int(os.environ.get("SHELF_SCAN_DAILY_LIMIT_PER_USER", "30"))
# How long an in-flight marker is valid (90s) before we forget about it.
IN_FLIGHT_TTL_SECONDS = 90
# Window for the rate limiter (24h).
RATE_LIMIT_WINDOW_SECONDS = 86400


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


class ShelfScanRateLimited(ShelfScanError):
    """Raised when a user has exceeded DAILY_SCAN_LIMIT_PER_USER."""


class ShelfScanInProgress(ShelfScanError):
    """Raised when a user already has a scan in flight."""


class ShelfScanService:
    """Orchestrator for the bookshelf-scanner feature.

    Wires AIService (vision LLM) → unified_metadata (fuzzy match) →
    simplified_book_service (bulk add) into a single user-facing flow.
    """

    def __init__(self):
        self._scan_store: Dict[str, Dict[str, Any]] = {}
        self._scan_store_lock = threading.RLock()
        self._rate_limit: Dict[str, list] = {}            # user_id -> [ts, ts, ...] (last 24h only)
        self._rate_limit_lock = threading.RLock()
        self._in_flight: Dict[str, float] = {}            # user_id -> start_ts
        self._in_flight_lock = threading.RLock()

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

    # ---- Enrichment helpers -----------------------------------------------

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
