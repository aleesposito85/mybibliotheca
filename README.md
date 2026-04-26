> **Fork notice.** This is a maintained fork of the original
> [`pickles4evaaaa/mybibliotheca`](https://github.com/pickles4evaaaa/mybibliotheca),
> which the upstream author flagged as no longer maintained. This fork adds
> security/data-integrity fixes from a full code audit, a graph-based
> recommendations feature ("Discover" page), and a bookshelf scanner that
> reads spines from a single photo via a multimodal vision LLM. Pre-built
> Docker images are published to GHCR — see
> [Docker quick start](#-docker-quick-start-pre-built-image) and
> [What's new in this fork](#-whats-new-in-this-fork) below.

# 📚 MyBibliotheca

# 2.0.1+
**⚠️ Warning**: MyBibliotheca is under heavy development. Always back up your data before upgrading. The developers do not guarantee data persistence or error-free operation. Please submit issues to the repository, and we will address them as soon as possible.

**MyBibliotheca** is a self-hosted personal library and reading-tracker—your open-source alternative to Goodreads, StoryGraph, and Fable! It lets you log, organize, and visualize your reading journey. Add books by ISBN, track reading progress, log daily reading, and generate monthly wrap-up images of your finished titles.

🆕 **Multi-User Features**: Multi-user authentication, user data isolation, admin management, and secure password handling.

[![Documentation](https://img.shields.io/badge/Documentation-MyBibliotheca-4a90e2?style=for-the-badge&logo=read-the-docs&logoColor=white)](https://mybibliotheca.org)

[![Discord](https://img.shields.io/badge/Discord-7289DA?logo=discord&logoColor=white&labelColor=7289DA&style=for-the-badge)](https://discord.gg/Hc8C5eRm7Q)

---

## 📸 Screenshots

### Library Homepage
Browse your personal book collection with beautiful cover displays, reading status indicators, and quick access to all your books.

![Library Homepage](https://i.imgur.com/cDN06Lo.png)

### Reading Log
Track your reading sessions with detailed logging including pages read, time spent, and personal notes for every book.

![Reading Log](https://i.imgur.com/1WqQQAW.png)

### Book Details
View comprehensive book information including genres, authors, reading status, publication dates, and manage your personal collection.

![Book Details](https://i.imgur.com/A4jI2nS.png)

---


---

## ✨ Features

- 📖 **Add Books**: Add books quickly by ISBN with automatic cover and metadata fetching. Now featuring bulk-import from Goodreads and other CSV files!
- ✅ **Track Progress**: Mark books as *Currently Reading*, *Plan to Read*, *Finished*, or *Library Only*.
- 📅 **Reading Logs**: Log daily reading activity and maintain streaks.
-  **Search**: Find and import books using the Google Books API.
- 📱 **Responsive UI**: Clean, mobile-friendly interface built with Bootstrap.
- 🔐 **Multi-User Support**: Secure authentication with user data isolation
- 👤 **Admin Management**: Administrative tools and user management
- 🔗 **Graph Database**: Powered by KuzuDB for advanced relationship modeling and queries
- 🧭 **Discover (recommendations)** *(new in this fork)*: graph-based "you might also enjoy" suggestions on the book detail page, library row, and a dedicated `/recommendations` page. Combines content signals (shared authors, categories, series) with an aggregate co-reader signal floored at ≥3 distinct readers for privacy.
- 📷 **Scan Shelf** *(new in this fork)*: snap a photo of a bookshelf and a multimodal vision LLM (local Ollama or any OpenAI-compatible cloud endpoint) extracts every readable spine, fuzzy-matches each title against the unified metadata pipeline, and lets you pick which detections to bulk-import.

## 🚀 Docker quick start (pre-built image)

Pre-built multi-arch (amd64 + arm64) images are published to GitHub Container Registry on every push to `main`. To run without cloning:

```bash
mkdir mybibliotheca && cd mybibliotheca
mkdir data
cat > .env <<'EOF'
SECRET_KEY=$(openssl rand -hex 32)
SECURITY_PASSWORD_SALT=$(openssl rand -hex 16)
EOF
curl -fsSL https://raw.githubusercontent.com/aleesposito85/mybibliotheca/main/docker-compose.yml -o docker-compose.yml
docker compose up -d
```

Then visit `http://localhost:5054`. Image: [`ghcr.io/aleesposito85/mybibliotheca:latest`](https://github.com/aleesposito85/mybibliotheca/pkgs/container/mybibliotheca).

For upstream's documentation site see [mybibliotheca.org](https://mybibliotheca.org/).


## 🗂️ Project Structure

```
mybibliotheca/
├── app/
│   ├── __init__.py              # Application factory
│   ├── auth.py                  # Authentication routes
│   ├── domain/                  # Domain models and business logic
│   ├── infrastructure/          # KuzuDB connection and repositories
│   ├── routes/                  # Application routes
│   ├── services/                # Business logic services
│   ├── schema/                  # Database schema definitions
│   ├── templates/               # Jinja2 templates
│   ├── static/                  # Static assets (CSS, JS, images)
│   └── utils/                   # Utility functions
├── data/                        # Data directory (mounted volume)
│   ├── kuzu/                    # KuzuDB database files
│   ├── covers/                  # Book cover images
│   └── uploads/                 # User uploaded files
├── scripts/                     # Admin and utility scripts
├── docs/                        # Documentation
├── docker-compose.yml           # Docker Compose configuration
├── Dockerfile                   # Docker image definition
├── requirements.txt             # Python dependencies
├── run.py                       # Application entry point
└── README.md                    # This file
```

---

## 🚀 What's new in this fork

This fork landed two large pieces of work on top of the upstream codebase.
Design and implementation artefacts live under
[`docs/superpowers/specs/`](docs/superpowers/specs/) and
[`docs/superpowers/plans/`](docs/superpowers/plans/).

### 1. Bug audit + fixes (≈45 issues)

A full pass over auth, data integrity, and operational concerns. Highlights:

- **Auth & sessions**
  - Session fixation mitigated — `session.clear()` and id rotation on login.
  - `next=` redirect rejects protocol-relative URLs (`//evil.com`).
  - CSRF-fail redirect validates same-origin Referer (was an open redirect).
  - Forced password change check moved before the `/api/*` short-circuit so
    flagged users can't keep using the JSON API.
  - API token now requires an explicit `API_TOKEN_USER_ID` to bind a real
    user; the hardcoded `dev-token-12345` fallback was removed.
  - Onboarding hashes the admin password at step 1 instead of caching the
    plaintext in the filesystem-backed Flask session.
  - `/admin/api/users/<id>/delete` was skipping the admin-password check
    due to operator precedence — now properly verified.
  - `/auth/setup/status` and `/auth/debug/user-count` are admin-gated.
- **API endpoints that lied about success**
  - `DELETE /api/v1/reading-logs/<id>` ignored `log_id` and returned 200 —
    now wired to the service.
  - `GET /api/v1/reading-logs` returned a hard-coded `[]` — now reads.
  - `PUT /api/v1/books/<id>` forwarded raw JSON to the DB layer — now
    type-coerced + whitelisted.
- **Data integrity**
  - V2 SQLite-import duplicate-book mapping bug (every duplicate linked to
    one arbitrary new id) — fixed.
  - Reading-log defaults `pages_read=1, minutes_read=1` (which silently
    invented data on legacy migrations) → `0`.
  - Series migration adds missing properties via additive ALTERs and
    preserves half-volume precision (was truncating `2.5` → `2`).
- **Security**
  - `restore_covers.py` Cypher injection (f-string interpolation) → parameterised.
  - Stored XSS in `library-indexed.js` (`innerHTML` of imported titles) —
    rebuilt with safe DOM APIs.
  - Pillow decompression-bomb cap (`MAX_IMAGE_PIXELS = 30M` + `verify()`).
  - SSRF / DNS-rebinding mitigation: pin the resolved IP before fetching;
    cover all loopback variants (127.x, `[::1]`, etc.).
  - `BEHIND_HTTPS` env knob defaulting to secure cookies + strict CSRF when
    not in `FLASK_DEBUG`.
  - `MAX_CONTENT_LENGTH` upload cap (16 MB default).
  - OPDS / Audiobookshelf settings JSON written atomically with `chmod 0600`.
  - Markdown autolink filter strips `javascript:` / `data:` / `vbscript:` hrefs.
- **Performance & correctness**
  - `simple_cache` `MISS` sentinel so functions returning `None` don't
    bypass the cache.
  - ISBN-10 / ISBN-13 round-tripping now refuses to synthesize fake ISBN-10s
    for `979`-prefix books (no equivalent exists).
  - OCR ISBN validator performs the mod-10 / mod-11 checksums.
  - OpenLibrary search requests `fields=...,isbn` explicitly (the default
    response excludes ISBN, which silently dropped every result when
    `isbn_required=true`).
  - Stacking-context fix on `.container` so Bootstrap modals stay clickable
    (was being trapped inside the page wrapper's `backdrop-filter`).

### 2. Graph-based recommendations ("Discover")

A new feature surfaced in three places:

- **Book detail page** — a "More like this" card lazy-fetched via
  `GET /recommendations/api/more-like-this/<book_id>`.
- **Library page** — top row "Recommended for you" plus an empty-state seed
  that appears when a brand-new user opens an empty library.
- **`/recommendations` page** — a dedicated discovery dashboard with
  "Top picks for you", "Continue your series", and "Popular" sections.

How it works:

- **Signals** — shared authors / categories / series (+next-volume bonus) and
  an aggregate co-reader signal computed from
  `(anchor)<-[:HAS_PERSONAL_METADATA]-(u:User)-[:HAS_PERSONAL_METADATA]->(c:Book)`.
- **Privacy** — the co-reader signal is floored at ≥3 distinct readers so
  small private libraries can't be reverse-engineered.
- **Cold-start** — falls back to a global "popular" list (cross-user
  finished-count) until the requesting user has ≥2 finished books.
- **Performance** — real-time + cached in `simple_cache` keyed by the
  existing per-user library version. No background jobs, no new schema,
  reuses `bump_user_library_version()` for invalidation.
- **Tunable** — every weight (`RECS_WEIGHT_AUTHOR`, `RECS_WEIGHT_SERIES`,
  ...) is overridable via env var without redeploying.

Tests: 35 new ones across `tests/test_recommendation_scorer.py`,
`tests/test_recommendation_service.py`, and
`tests/test_recommendation_routes.py`. The service-level tests use a new
in-memory KuzuDB fixture in `tests/conftest.py` that's reusable for any
future graph-touching tests.

Related files:

- Service: [`app/services/kuzu_recommendation_service.py`](app/services/kuzu_recommendation_service.py)
- Routes: [`app/routes/recommendation_routes.py`](app/routes/recommendation_routes.py)
- Templates: [`app/templates/recommendations.html`](app/templates/recommendations.html), [`app/templates/_recommendation_card.html`](app/templates/_recommendation_card.html)
- Design spec: [`docs/superpowers/specs/2026-04-25-recommendations-design.md`](docs/superpowers/specs/2026-04-25-recommendations-design.md)
- Implementation plan: [`docs/superpowers/plans/2026-04-25-recommendations.md`](docs/superpowers/plans/2026-04-25-recommendations.md)

### 3. Bookshelf scanner

A "Scan Shelf" card on the Add Book page accepts a photo of a bookshelf,
sends it to a multimodal vision LLM, and turns each readable spine into an
import candidate. The whole flow is privacy-first: by default it talks to
a local Ollama instance, with an OpenAI-compatible cloud endpoint as an
optional fallback.

How it works:

- **Image preprocessing** — Pillow validates the upload, applies EXIF
  orientation, downscales to a max edge, and re-encodes to JPEG. The
  decompression-bomb cap is lifted to 250M pixels locally so that modern
  phone cameras work, while still rejecting truly malicious payloads.
- **Vision extraction** — `AIService.extract_books_from_shelf_image()`
  reuses the same provider-selection + fallback logic as single-cover
  extraction. The prompt at
  [`prompts/shelf_scan.mustache`](prompts/shelf_scan.mustache) was tuned
  to keep cloud models from bailing with `{"books": []}` (positive
  framing, inline JSON example, explicit anti-empty assertion). Local
  Ollama defaults to `qwen3-vl:8b`; cloud has been validated against
  `qwen3-vl:235b-instruct` on Ollama Cloud.
- **Parser** — pure-Python parser handles the dozen JSON variants real
  models emit (markdown fences, prose preambles, single-book objects,
  comments, trailing commas) and normalises into a canonical
  `{title, author, spine_position, confidence}` shape.
- **Per-detection enrichment** — each detection runs through
  `unified_metadata.fetch_unified_by_title()` (Google Books +
  OpenLibrary) in a small parallel pool, with confidence scoring
  comparing the LLM's title against each candidate.
- **Confirmation UI** — a pre-enriched grid shows cover / metadata /
  confidence for every detection; the user picks which ones to import.
  Nothing is written to the DB until confirmation.
- **Bulk-add** — the confirmed selections feed `safe_import_manager`,
  reusing the existing CSV-import async progress page (with a banner
  marking the source as `shelf_scan`).
- **Configuration** — provider, base URL, API key, model, and timeouts
  all flow through the existing admin AI settings page (no separate
  config). A dedicated `SHELF_SCAN_AI_TIMEOUT` (default 180s) overrides
  the shorter timeout used for single-cover extraction.

Endpoints (all under `/books/scan/`):

- `GET /upload` — upload form
- `POST /upload` — preprocess + run extraction synchronously
- `GET /confirm/<scan_id>` — pre-enriched candidate grid
- `POST /confirm/<scan_id>` — kick off async bulk-add
- `POST /discard/<scan_id>` — drop the scan store entry

Tests: 47 across `tests/test_shelf_scan_service.py`,
`tests/test_shelf_scan_parser.py`, `tests/test_shelf_scan_aiservice.py`,
and `tests/test_shelf_scan_routes.py`.

Related files:

- Service: [`app/services/shelf_scan_service.py`](app/services/shelf_scan_service.py)
- AI extension: [`app/services/ai_service.py`](app/services/ai_service.py)
- Routes: [`app/routes/shelf_scan_routes.py`](app/routes/shelf_scan_routes.py)
- Templates: [`app/templates/shelf_scan_upload.html`](app/templates/shelf_scan_upload.html), [`app/templates/shelf_scan_confirm.html`](app/templates/shelf_scan_confirm.html)
- Prompt: [`prompts/shelf_scan.mustache`](prompts/shelf_scan.mustache)
- Design spec: [`docs/superpowers/specs/2026-04-26-bookshelf-scanner-design.md`](docs/superpowers/specs/2026-04-26-bookshelf-scanner-design.md)
- Implementation plan: [`docs/superpowers/plans/2026-04-26-bookshelf-scanner.md`](docs/superpowers/plans/2026-04-26-bookshelf-scanner.md)

---

## 📄 License

Licensed under the [MIT License](LICENSE).

---

## ❤️ Contribute

**MyBibliotheca** is open source and contributions are welcome!

- 🐛 **Report Bugs**: Open an issue on GitHub
- 💡 **Feature Requests**: Submit ideas for new features
- 🔧 **Pull Requests**: Contribute code improvements
- 📖 **Documentation**: Help improve our docs
- 💬 **Community**: Join our [Discord](https://discord.gg/Hc8C5eRm7Q)

### Development Setup

```bash
# Fork and clone the repository
git clone https://github.com/pickles4evaaaa/mybibliotheca.git
cd mybibliotheca

# Create a branch for your changes
git checkout -b feature/my-new-feature

# Make your changes and test
docker compose -f docker-compose.dev.yml up -d

# Submit a pull request
```
---

### 📞 Getting Help

If you encounter issues:

1. **Check the logs**: `docker compose logs -f`
2. **Enable debug mode**: Add `MYBIBLIOTHECA_DEBUG=true` to `.env` and restart
3. **Search existing issues**: [GitHub Issues](https://github.com/pickles4evaaaa/mybibliotheca/issues)
4. **Ask for help**: [Discord Community](https://discord.gg/Hc8C5eRm7Q)
5. **Create an issue**: Include logs, environment details, and steps to reproduce

---
