# Bookshelf Scanner — Design

**Date:** 2026-04-26
**Status:** Approved (design phase). Ready for implementation plan.
**Author:** drafted via Claude brainstorming skill, approved by repo owner

## Summary

Add a "Scan Bookshelf" feature that lets a user upload a photo of their
bookshelf, identifies every visible spine via a multimodal vision LLM,
fuzzy-matches each detection against the existing `unified_metadata`
service, and bulk-adds the user-confirmed books through the existing import
pipeline. Privacy-first: defaults to a self-hosted Ollama backend, with
optional cloud (OpenAI Vision) opt-in via env var.

## Goals

- Reduce the friction of seeding a library from a physical bookshelf —
  one photo replaces 20+ manual ISBN entries.
- Reuse every existing primitive that fits: the `AIService` multimodal
  client, `unified_metadata`, `simplified_book_service.create_standalone_book`,
  `safe_import_manager` for async progress tracking, and the `image_processing`
  preprocessing utilities.
- Privacy + cost: Ollama by default, no per-user API key UI in v1.
- High user trust: never auto-import. Confirmation grid is mandatory; the
  default-selected behaviour is conservative (only `confidence=high` matched
  detections are pre-checked).

## Non-goals (v1)

- **Front-out covers** — only spines are in scope. Mixed-display shelves
  will skip face-out books. Adding cover-face support is one prompt
  edit when we want it.
- **Multi-image scan sessions** — one photo per scan. Wall-sized bookcases
  require multiple separate scans in v1.
- **Per-user backend config UI** — admin sets `AI_PROVIDER` env var; all
  users use it. Per-user API keys can come later.
- **Pre-computed bounding boxes** — no clickable spine highlights on the
  uploaded photo. The detected list is just `spine_position` ordered.
- **Cost estimation** beyond a static "~$0.02 per scan" notice when
  `AI_PROVIDER=openai`.
- **Re-scan-with-different-model** — the upload page raw photo is kept on
  disk for 1h but no UI exposes "re-run with a different model" yet.

## Surfaces

### 1. Add Book page — "Scan Shelf" card

- File: `app/templates/add_book.html`
- New card alongside the existing "ISBN Lookup" and "File Upload" cards in
  the Quick Add Options sidebar. Heading: **📷 Scan Shelf**. Body:
  "Upload a photo of your bookshelf and let AI identify multiple books at
  once." Single CTA button: **Open Scanner →** linking to `/books/scan/`.

### 2. Upload page

- File: `app/templates/shelf_scan_upload.html`
- Single Bootstrap card, three regions:
  - Header — one-liner "Take or upload a clear, well-lit photo of your bookshelf — we'll identify multiple books at once."
  - Drag-and-drop zone — `image/jpeg, image/png, image/webp`. Client-side
    preview thumbnail on file select.
  - Backend notice — server-rendered string telling the user which provider
    is configured and (when OpenAI) the per-scan cost estimate. If neither
    provider is configured, a yellow callout disables the submit button
    and links to the docs.
- Submit button toggles to "Scanning… 30–80 seconds" with a spinner during
  the in-flight POST.
- On error (LLM unavailable / 0 spines detected) the page re-renders with
  the original upload preview retained and an error banner above the
  upload zone — user can retry without re-picking the file.

### 3. Confirmation page

- File: `app/templates/shelf_scan_confirm.html`
- Header: shelf-thumbnail (clickable to open lightbox of the original
  photo) + a summary panel — `Detected N`, `Matched M`, `Already in your
  library: K`, `Unmatched: U`.
- Bulk-action toolbar: "Select all", "Select none", "Only high confidence".
- Card grid (Bootstrap responsive, 4 columns desktop / 2 mobile). One card
  per detection.
- Card mechanics:
  - Cover image (or placeholder if unmatched), spine_position label, title
    + author, confidence badge (green/yellow/red).
  - Checkbox; default-checked when `confidence=high AND matched`.
  - Alternative-edition dropdown (`▼ alts`) — only when `len(alternatives) > 0`.
    Picking an alternative swaps the displayed cover/title/ISBN; the chosen
    alternative index is sent in the form's `overrides` JSON blob.
  - Unmatched cards: no cover, "Couldn't find in metadata" message, the
    checkbox is **disabled** (not just unchecked) — bulk-add can only
    handle matched candidates. A "Search manually" link to
    `/books/add?title=<detected>` opens in a new tab so the user can
    finish the rest of the confirmation flow without losing state.
- Submit button label updates live: "Add **N** selected books →". Disabled
  when N=0. POST to `/books/scan/confirm` returns `{status, task_id}` on
  success and the JS redirects to the existing import progress page.

### 4. Progress page

- Reuse the existing CSV-import progress template
  (`app/templates/import_books_progress.html`).
- Detect a shelf-scan job via `job['source'] == 'shelf_scan'` and render a
  small banner: "Importing N books from shelf scan".

## Architecture

```
app/
├── services/
│   ├── ai_service.py                       # MODIFY — add extract_books_from_shelf_image()
│   └── shelf_scan_service.py               # NEW — orchestrator
├── routes/
│   └── shelf_scan_routes.py                # NEW — 4 endpoints
├── templates/
│   ├── shelf_scan_upload.html              # NEW
│   ├── shelf_scan_confirm.html             # NEW
│   └── add_book.html                       # MODIFY — add Scan Shelf card
└── utils/
    ├── safe_import_manager.py              # REUSED for async bulk-add progress
    ├── image_processing.py                 # REUSED for resize + validate
    └── unified_metadata.py                 # REUSED for fuzzy match
```

Modifications to existing files:

- `app/services/ai_service.py` — add `extract_books_from_shelf_image()`.
- `app/services/__init__.py` — export `shelf_scan_service` lazy singleton.
- `app/routes/__init__.py` — register `shelf_scan_bp` in `register_blueprints`.
- `app/templates/add_book.html` — add the Scan Shelf card.

### Public service API

```python
class ShelfScanService:
    def scan_image_and_enrich_sync(
        self,
        image_bytes: bytes,
        user_id: str,
        original_filename: str,
    ) -> dict: ...
        # Returns: {scan_id, candidates: [...], summary: {...}, preview_url}

    def get_scan(self, scan_id: str, user_id: str) -> dict | None: ...
        # Returns the stored candidates if scan_id is valid, owned by user_id,
        # and not expired. Otherwise None.

    def start_bulk_add_async(
        self,
        user_id: str,
        scan_id: str,
        picked: list[str],
        overrides: dict[str, int],
    ) -> str: ...
        # Returns task_id. Job runs through safe_import_manager.

    def discard_scan(self, scan_id: str, user_id: str) -> bool: ...
        # Removes scan_id from store + deletes preview file.


class AIService:
    # NEW method, alongside the existing extract_book_info_from_image
    def extract_books_from_shelf_image(self, image_bytes: bytes) -> list[dict]: ...
        # Returns: [{title, author, spine_position, confidence}, ...]
```

### Candidate dict shape

```python
{
    "detection_id": "det_001",
    "spine_position": 3,
    "confidence": "high" | "medium" | "low",
    "detected": {
        "title": "<as printed on spine>",
        "author": "<as printed; '' if not visible>",
    },
    "matched": True,                       # did unified_metadata return anything?
    "best_match": {                        # populated when matched=True
        "title": "Dune",
        "authors": ["Frank Herbert"],
        "isbn13": "9780441172719",
        "isbn10": "0441172717",
        "cover_url": "https://...",
        "published_date": "1990-09-01",
        "page_count": 535,
        "language": "en",
        "description": "...",
        "similarity_score": 0.97,
    },
    "alternatives": [...],                 # up to 4 other candidates from the
                                            # same fetch_unified_by_title call —
                                            # best_match is index 0 of the search
                                            # results, alternatives are 1..4.
                                            # Same shape as best_match per entry.
    "default_selected": True,              # high+matched → True; else False
}
```

## LLM prompt + JSON contract

Prompt (lightly adapted per provider chat-format):

```
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

Per-provider tuning:

- **Ollama (Llama3.2-Vision / Qwen2.5-VL)** — append `"Return ONLY the JSON
  object, no markdown, no commentary."` Strip leading prose up to the first
  `{` and trailing prose after the last `}` before parsing.
- **OpenAI (gpt-4o-mini etc.)** — set
  `response_format={"type": "json_object"}` so the API guarantees JSON. No
  fence-stripping needed.

Parser (`_parse_shelf_response`):

1. Strip Markdown code fences.
2. `json.loads` with try/except → return `[]` on parse failure.
3. Validate shape: must be `{"books": [...]}`; coerce missing keys.
4. Per-book validation:
   - `title` required (non-empty after strip).
   - `author` defaults to `""`.
   - `spine_position` coerced to int; defaults to enumeration index.
   - `confidence` ∈ {high,medium,low}; defaults to `medium`.
5. Drop books with empty `title`.
6. Return list[dict] in `spine_position` order.

We deliberately do NOT ask the LLM for ISBN — it would happily hallucinate
plausible-looking ones. ISBNs come authoritatively from `unified_metadata`
during enrichment.

## Routes

```python
shelf_scan_bp = Blueprint("shelf_scan", __name__, url_prefix="/books/scan")

@shelf_scan_bp.route("/", methods=["GET"])              # upload page
@shelf_scan_bp.route("/upload", methods=["POST"])       # multipart; sync ~30-80s
@shelf_scan_bp.route("/confirm", methods=["POST"])      # kicks off async bulk-add
@shelf_scan_bp.route("/progress/<task_id>")             # JSON poll (re-uses safe_import_manager)
@shelf_scan_bp.route("/<scan_id>/discard", methods=["POST"])  # frees scan_store + preview file
```

Auth: every route is `@login_required`.

`/upload` flow (sync, returns rendered HTML):

1. `request.files['shelf_image']` — validate format via PIL.
2. Read bytes (Flask enforces `MAX_CONTENT_LENGTH=16MB` before we get them).
3. Hand off to `shelf_scan_service.scan_image_and_enrich_sync`.
4. Render `shelf_scan_confirm.html` on success, `shelf_scan_upload.html`
   with the original preview retained on `ShelfScanLLMUnavailable` /
   `ShelfScanEmptyResult`.

`/confirm` flow (async kickoff, returns JSON):

1. Look up scan in store; 410 if missing/expired/not-owned.
2. Call `start_bulk_add_async` with `picked` ids and `overrides`.
3. Return `{status: 'success', task_id}`. JS redirects to the existing
   progress page.

## Image preprocessing

`ShelfScanService._preprocess(image_bytes, original_filename) -> tuple[bytes, str]`:

1. Open with PIL, validate format ∈ {JPEG, PNG, WebP} via `Image.open(...).format`.
2. `Image.MAX_IMAGE_PIXELS = 30_000_000` is already set globally by the
   audit fix in `image_processing.py` — bomb-proof.
3. Resize so the longer edge is ≤2048px. Drops typical phone-photo payload
   ~10×; no measurable spine-recognition accuracy loss.
4. Encode as JPEG q=85.
5. Save the resized bytes to `data/uploads/scans/<scan_id>.jpg` and return
   both the bytes (for the LLM call) and the relative URL (for the preview
   image on the confirmation page).

Note: the preview file is NOT the full-resolution original — it's the
already-resized version. This is a minor storage optimisation; if we want
"re-run scan with different model" later, we'd save the original instead.

## Scan store

In-memory dict in `ShelfScanService` instance. Fine because the app runs
`WORKERS=1` (KuzuDB single-process constraint).

```python
self._scan_store: dict[str, dict] = {}   # scan_id -> {user_id, candidates, expires_at}
self._scan_store_lock = threading.RLock()

# TTL: 1h. Eviction: lazy on access + sweep on each new scan insert.
```

If we ever scale out, this moves to Redis. No need now.

## Concurrency guardrails

- **One scan in flight per user**: `_scan_in_flight: dict[str, float]`
  (user_id → start_ts). On upload, refuse with `409` and a friendly
  "Scan already in progress" if the user has an active scan less than 90s
  old. Cleared at the end of `scan_image_and_enrich_sync`.
- **Bounded enrichment concurrency**: `ThreadPoolExecutor(max_workers=4)`
  for parallel `fetch_unified_by_title` calls. Avoids hammering Google
  Books / OpenLibrary.

## Cost guard for cloud LLM

- Resize-before-send (already required for latency anyway) is the main
  cost lever — the typical 8MB phone photo becomes ~250KB before transit.
- Server-side rate limit: max **30 scans per user per 24h** via a counter
  stored alongside the scan store (`_user_scan_count: dict[user_id, list[ts]]`,
  trimmed to last 24h on each insert). Returns `429` if exceeded.
  Override via env: `SHELF_SCAN_DAILY_LIMIT_PER_USER` (default 30).
- The upload page shows "~$0.02 per scan" only when `AI_PROVIDER=openai`.

## Bulk-add worker

Runs in the same thread pattern `safe_import_manager` already uses for CSV
import:

```python
def _bulk_add_worker(user_id, scan_id, picked, overrides):
    safe_update_import_job(user_id, task_id, {"status": "running", "total": len(picked)})
    successes, errors = 0, []
    for det_id in picked:
        try:
            book_data = self._resolve_candidate(scan_id, det_id, overrides)
            book_id = simplified_book_service.create_standalone_book(book_data)
            self._link_to_user_library(user_id, book_id, status="library_only")
            successes += 1
            safe_update_import_job(user_id, task_id, {
                "processed": successes + len(errors),
                "success": successes,
                "current_book": book_data["title"],
            })
        except Exception as e:
            logger.exception("shelf_scan: failed to add %s", det_id)
            errors.append({"detection_id": det_id, "error": str(e)})
            safe_update_import_job(user_id, task_id, {
                "processed": successes + len(errors),
                "errors": len(errors),
                "error_messages": errors,
            })
    safe_update_import_job(user_id, task_id, {"status": "completed"})
```

One failed book doesn't abort the rest. The progress page surfaces errors
in an expandable "Some books couldn't be added" panel (already present
in the existing template).

Each newly-created book is linked into the user's library with
`reading_status="library_only"` via `HAS_PERSONAL_METADATA` so the user
sees them on `/library` immediately.

## Error handling

| Error                              | Where caught               | User-facing behaviour                                         |
| ---------------------------------- | -------------------------- | ------------------------------------------------------------- |
| Invalid image format               | route                      | Flash + redirect to upload page                               |
| `MAX_CONTENT_LENGTH` exceeded      | Flask                       | Default Flask 413; we don't intercept                         |
| `AI_PROVIDER` not configured       | service                    | Yellow notice on upload page; submit disabled                 |
| Vision LLM 5xx / timeout           | service (existing fallback) | Existing `AI_FALLBACK_ENABLED` path retries other provider    |
| Both providers fail                | service                    | `ShelfScanLLMUnavailable` → friendly error, retain preview    |
| LLM returns 0 books                | service                    | `ShelfScanEmptyResult` → friendly error, retain preview       |
| LLM JSON unparseable               | parser                     | Treated as 0 books → same as above                            |
| `unified_metadata` upstream slow   | enrichment                 | 4-worker pool; per-call 10s timeout (existing)                |
| `unified_metadata` returns nothing | enrichment                 | candidate `matched=False`; surfaces in confirmation grid      |
| Scan store TTL expired             | confirm route              | 410 Gone with friendly "Scan expired, please re-upload"       |
| Scan store cross-user access       | confirm route              | 410 Gone (don't leak existence)                               |
| One book fails in bulk-add         | bulk worker                | Logged + listed; other books still added                      |
| All books fail in bulk-add         | bulk worker                | Job status `completed` with all errors; user sees full report |

## Health check

`GET /admin/scan/health` (admin-only):

- Probes the configured `AI_PROVIDER` with a tiny 1×1 dummy image.
- Returns `{provider, model, ok, latency_ms, error}`.
- Result cached for 5 minutes to avoid hammering the LLM.

Useful for verifying Ollama connectivity from inside the Docker container
without uploading a real photo.

## Observability

Per-scan log line:

```
[shelf_scan] user=<id> provider=<ollama|openai> model=<name>
            detected=<n> matched=<n> already_owned=<k> unmatched=<u>
            preprocess_ms=<n> llm_ms=<n> enrich_ms=<n> total_ms=<n>
```

The bulk-add job dict includes `source: 'shelf_scan'` and `scan_id` so
the existing progress page can show context.

All exception paths use `logger.exception(...)` (the audit established this
pattern); no silent except.

## Testing strategy

### Unit tests — parser

`tests/test_shelf_scan_parser.py` (pure Python). ~12 cases:

- Happy path
- Markdown fences
- Leading prose
- Trailing prose
- Empty `{"books":[]}`
- Garbage string
- Missing required field per book
- Bogus `confidence` value
- Missing `spine_position`
- Single-book JSON not wrapped in `books`
- Empty title (dropped)
- Out-of-order `spine_position` (sorted on the way out)

### Service-level tests

`tests/test_shelf_scan_service.py` against the in-memory Kuzu fixture in
`tests/conftest.py`. Mocks `AIService.extract_books_from_shelf_image` and
`fetch_unified_by_title`. Cases:

- Happy path — 5 detected → 5 enriched
- Already-owned filter
- Unmatched book — present with `matched=False`, `default_selected=False`
- Empty LLM result → `ShelfScanEmptyResult`
- LLM unavailable → `ShelfScanLLMUnavailable`
- Scan store TTL → entry purged
- Per-user isolation — Bob can't see Alice's scan
- Bulk-add happy path — N picked → N books in graph + N personal-metadata
  edges with `library_only`
- Bulk-add per-book error — others still succeed; job `completed`, errors
  listed
- Override picking alternative — bulk-add uses alternative, not default

### Route smoke tests

`tests/test_shelf_scan_routes.py` (Flask test client; service mocked):

- `GET /` renders upload page; redirects when anonymous
- `POST /upload` happy path returns confirm HTML
- `POST /upload` with non-image errors gracefully (service NOT called)
- `POST /upload` with no file — flash + redirect
- `POST /confirm` valid → JSON `{status:success, task_id}`
- `POST /confirm` expired → 410
- `POST /confirm` cross-user → 410
- `GET /progress/<task_id>` → JSON status from `safe_import_manager`

### Out of v1 tests

- Real Ollama / OpenAI calls in CI
- Image preprocessing edge cases (covered indirectly by `image_processing.py`
  fixes from the audit)
- Multi-image session (out of v1 scope)
- Concurrency stress on `_scan_store` (single-worker app; lock is correct
  by inspection)

### Acceptance bar

- All three test files pass against the existing fixtures.
- Manual end-to-end on dev container with a real Ollama call shows the
  full flow (upload → confirm → progress → library) for at least one real
  bookshelf photo.

## Operational notes

- KuzuDB single-worker constraint preserved — no parallel queries, no
  background process spawned outside the existing import-manager pattern.
- First-request latency: 30–80s for a 20-book shelf (5–30s LLM + 10–25s
  parallel enrichment + sub-second filter/serialize). Acceptable for a
  deliberate "scan my shelf" action.
- Bulk-add stage is async; user can navigate away from the progress page.
- No new dependencies. No new schema. No migrations.
- Reuses the audit-era preprocess pipeline (`MAX_IMAGE_PIXELS=30M`,
  `verify()`, `MAX_CONTENT_LENGTH=16MB`).

## Decisions log

| Question                              | Decision                                                |
| ------------------------------------- | ------------------------------------------------------- |
| Scope of v1                           | Spines only; no front-out covers; one image per session |
| Surface placement                     | Sub-action on Add Book page (no nav slot, no library CTA in v1) |
| Sync vs async                         | LLM call sync; bulk-add async via `safe_import_manager` |
| Confirmation grid                     | Pre-enriched in one combined request                     |
| Backend service abstraction           | Extend `AIService` with one new method                   |
| Backend config                        | Existing env vars (no per-user UI in v1)                |
| LLM prompt JSON contract              | `{books: [{title, author, spine_position, confidence}]}` |
| ISBN extraction                       | Never ask the LLM; always derive from `unified_metadata` |
| Hide already-owned                    | Yes by default                                          |
| Image resize cap                      | 2048px long edge, JPEG q=85                              |
| Scan store TTL                        | 1h, in-memory                                           |
| Per-user rate limit                   | 30 scans / 24h, env-overridable                         |
| Default-selected per card             | `confidence=high AND matched`                           |
| Failure mode of one book in bulk-add  | Continue; report per-book error                         |
| Health check route                    | `/admin/scan/health`, admin-only, 5m cache              |
