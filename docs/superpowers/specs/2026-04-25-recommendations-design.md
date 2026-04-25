# Graph-Based Book Recommendations — Design

**Date:** 2026-04-25
**Status:** Approved (design phase). Ready for implementation plan.
**Author:** drafted via Claude brainstorming skill, approved by repo owner

## Summary

Add a "you might also enjoy" recommendations feature that taps the existing
KuzuDB graph (Books, Persons via AUTHORED, Series via PART_OF_SERIES,
Categories via CATEGORIZED_AS, and per-user reading via HAS_PERSONAL_METADATA).
The feature surfaces in three places — the book detail page ("More like this"
card), the library page (top row + empty-state seed), and a new dedicated
`/recommendations` page — with content-based and aggregate-collaborative
signals, real-time computation cached by the existing user-library version,
and a cold-start fallback to globally popular books.

## Goals

- Surface a useful "what should I read next" answer at three points in the app
  with progressively richer context: per-book, per-library, and dedicated.
- Use the underutilized graph relationships (author, series, category,
  co-reader) without introducing a new persistence layer.
- Honor the existing single-worker KuzuDB constraint and per-user privacy
  flags. No new schema, no friend/follow edges.
- Ship a tunable, testable scorer rather than a black-box.

## Non-goals (v1)

- Vector / embedding-based "semantic" similarity. Premature given graph signals
  cover the obvious cases.
- A friend / follow / social layer. The graph has no such relationships today.
- Cross-user reveal of who read what. The collaborative signal is aggregate
  only and floored at ≥3 distinct readers per pair to avoid identifying
  small-library users.
- Pre-computed background jobs. Real-time + cached works at this scale.
- External API recs (Google Books / OpenLibrary suggestions for books not in
  our graph). The "universal library" model means anything worth recommending
  is something a user has already imported.

## Surfaces

### 1. Book detail — "More like this"

- File: `app/templates/view_book_enhanced.html`
- Inserted as a section below personal information, before footer.
- Heading: "More like this"
- Renders up to **8** cards (4 visible at once on desktop; horizontal scroll on
  mobile). Skeleton placeholder while async fetch resolves.
- Hidden entirely if fewer than 4 results — a near-empty section feels broken.
- Lazy-loaded via `GET /recommendations/api/more-like-this/<book_id>`.

### 2. Library page

- File: `app/templates/library.html`
- **Top row "Recommended for you"** above the filter bar, when the user has
  ≥1 finished book. Hidden when filtering or searching.
- **Empty state** — when the user has 0 books, the existing `.empty-state`
  block gets a sibling section "Popular books to start your library" with the
  popular fallback. Each card has a one-click "Add to library" affordance via
  the existing add flow.
- Lazy-loaded via `GET /recommendations/api/library-row`.

### 3. Dedicated `/recommendations` page

- File: `app/templates/recommendations.html`
- Linked from the main nav as **"Discover"**.
- Sections, in order:
  1. **Top picks for you** — up to 20 cards, scored. Heading swaps to "Popular
     among readers" when in cold-start (`personalized: false`), with a banner
     reading "Finish a couple of books to personalize this."
  2. **Continue your series** — only rendered if non-empty. Cards show
     "Volume {n} of {series_name}" instead of a generic recommendation reason.
  3. **Popular** — shown to all users (even personalized ones) as a discovery
     surface.
- Server-side rendered from a single `get_recommendations_page_sync` payload —
  no async fetches.
- Layout reserves space for a future "Filter by genre" dropdown but v1 ships
  without it.

### Card primitive

- Reused across all surfaces. Cover, title, author, one-line
  `recommendation_reason`. Click → `/book/<id>`.
- New partial template `app/templates/_recommendation_card.html`.
- ARIA: `aria-label="Recommendation: <title> by <author>. <reason>"`.

## Architecture

```
app/
├── services/
│   └── kuzu_recommendation_service.py     # NEW
├── routes/
│   └── recommendation_routes.py            # NEW
├── templates/
│   ├── recommendations.html                # NEW
│   └── _recommendation_card.html           # NEW partial
└── utils/
    └── simple_cache.py                     # REUSED, no changes
```

Modifications to existing files:

- `app/templates/view_book_enhanced.html` — append "More like this" card.
- `app/templates/library.html` — append top row + empty-state seeding.
- `app/__init__.py` — register the new blueprint.
- `app/services/__init__.py` — export `recommendation_service` singleton.
- `app/templates/base.html` — add "Discover" nav item.

### Public service API

```python
class KuzuRecommendationService:
    def get_more_like_this_sync(self, book_id: str, user_id: str, limit: int = 8) -> list[dict]: ...
    def get_library_row_sync(self, user_id: str, limit: int = 10) -> list[dict]: ...
    def get_top_picks_sync(self, user_id: str, limit: int = 20) -> list[dict]: ...
    def get_continue_series_sync(self, user_id: str, limit: int = 10) -> list[dict]: ...
    def get_popular_sync(self, user_id: str, limit: int = 20) -> list[dict]: ...
    def get_recommendations_page_sync(self, user_id: str) -> dict: ...
        # Returns: {"top_picks": [...], "continue_series": [...], "popular": [...], "personalized": bool}
```

Each list element is a dict shaped like the existing `Book` serializer output
(id, title, authors, cover_url, isbn13, isbn10, series, ...) plus:

- `recommendation_reason: str` — short string the UI shows under the cover
- `score: float` — present in debug mode only

## Signals & scoring

### Per-signal queries

Each signal is its own Cypher query returning `{candidate_book_id: int_value}`.
Merged in Python.

1. **Shared authors**
   ```cypher
   MATCH (a:Book) WHERE a.id IN $anchors
   MATCH (a)<-[:AUTHORED]-(p:Person)-[:AUTHORED]->(c:Book)
   WHERE c.id <> a.id
   RETURN c.id AS book_id, count(DISTINCT p) AS n
   ```
2. **Shared categories** — same shape, against `[:CATEGORIZED_AS]->(Category)<-[:CATEGORIZED_AS]`.
3. **Same series** — same shape, against `[:PART_OF_SERIES]->(Series)<-[:PART_OF_SERIES]`. Bonus extra weight if the candidate is the immediate next volume.
4. **Co-reader aggregate**
   ```cypher
   MATCH (a:Book) WHERE a.id IN $anchors
   MATCH (a)<-[m1:HAS_PERSONAL_METADATA]-(u:User)-[m2:HAS_PERSONAL_METADATA]->(c:Book)
   WHERE m1.finish_date IS NOT NULL AND m2.finish_date IS NOT NULL
     AND c.id <> a.id
   WITH c.id AS book_id, count(DISTINCT u) AS n
   WHERE n >= 3
   RETURN book_id, n
   ```
   The `n >= 3` floor is the privacy threshold — single-user / two-user reads
   never appear in the collab signal.
5. **Language match** (lightweight) — anchors and candidate share `language` → +constant.

### Weighted-sum score

```python
_RECS_WEIGHTS = {
    "author": 5.0,
    "category": 1.5,
    "series": 8.0,
    # Extra on top of "series" when the candidate's volume_number is exactly
    # one greater than the anchor's. Only applies in the single-anchor "More
    # like this" case; multi-anchor surfaces (top picks, library row) ignore
    # this weight because there's no single ordering to compare against.
    "series_next_volume": 4.0,
    "coreader": 2.0,             # multiplied by log(1 + count)
    "language": 0.5,
}

score = (
    W.author   * shared_authors
    + W.category * shared_categories
    + W.series   * (1 if same_series else 0)
    + W.series_next_volume * (1 if next_unread_volume else 0)
    + W.coreader * math.log(1 + coreader_count)
    + W.language * (1 if same_language else 0)
)
```

Weights live in a module-level dict that is env-overridable
(`RECS_WEIGHT_AUTHOR=5.0` etc.) so we can tune without redeploys.

### `recommendation_reason`

The dominant signal for a candidate (highest individual contribution) becomes
the reason string:

- Series → `"Same series as {anchor.title}"` or `"Volume {n} of {series.name}"`
- Author → `"By {author.name}"`
- Category → `"More {category.name}"`
- Coreader → `"Read by people who liked {anchor.title}"`
- Language fallback → unused as primary reason; never the dominant signal alone

The book detail card displays the reason; the library row hides it for
visual cleanliness.

### Filtering

After scoring, the candidate set is filtered to remove books the user already
has in their library:

```cypher
MATCH (u:User {id: $user_id})-[:HAS_PERSONAL_METADATA]->(b:Book)
RETURN b.id
```

Set difference in Python; sort desc by score; take top-N.

### Anchors per surface

- **More like this** — single anchor: the book on the page.
- **Top picks / library row** — last **5** finished books for the user, by
  `finish_date` desc. If <2 finished, switch to popular fallback.

### Dedicated queries (non-scoring)

- **Continue series** — return one card per started-but-incomplete series:
  the lowest-numbered unread volume. Sorted so the series with the most
  recently finished volume comes first. Two-step in Python:

  Step A — find started series and their most-recent finish date:
  ```cypher
  MATCH (u:User {id:$uid})-[m:HAS_PERSONAL_METADATA]->(read:Book)-[:PART_OF_SERIES]->(s:Series)
  WHERE m.finish_date IS NOT NULL
  RETURN s.id AS series_id, s.name AS series_name, max(m.finish_date) AS recent_finish
  ```

  Step B — for each series, fetch the lowest-volume book the user has not
  added (parameterized; one query per series, run in a small batch):
  ```cypher
  MATCH (s:Series {id: $series_id})<-[r:PART_OF_SERIES]-(next:Book)
  WHERE NOT EXISTS { MATCH (:User {id: $uid})-[:HAS_PERSONAL_METADATA]->(next) }
  RETURN next, r.volume_number AS volume_number
  ORDER BY r.volume_number ASC
  LIMIT 1
  ```

  Sort the resulting cards by `recent_finish` desc.

  The `recommendation_reason` for these cards is built directly as
  `"Volume {n} of {series_name}"` — these never go through the weighted
  scorer.

- **Popular global**
  ```cypher
  MATCH (b:Book)<-[m:HAS_PERSONAL_METADATA]-()
  WHERE m.finish_date IS NOT NULL
  WITH b, count(*) AS n
  ORDER BY n DESC
  LIMIT 50
  RETURN b, n
  ```
  Cold-start fallback. Excluded against user's library before serving.

## Cold-start

`_count_finished_for(user_id)` runs once per request, with a 60s TTL cache.
If `< 2`, every personalized surface returns the popular fallback with
`recommendation_reason = "Popular among readers"` and the page bundle's
`personalized` flag is `False`. The dedicated page shows a one-line banner
explaining the personalization gate.

## Caching

Reuses `app/utils/simple_cache.py` exactly as-is.

```python
def _ml_this_key(book_id, user_id):
    return f"recs:more_like_this:{user_id}:v{get_user_library_version(user_id)}:b{book_id}"

def _row_key(user_id):
    return f"recs:library_row:{user_id}:v{get_user_library_version(user_id)}"

def _page_key(user_id):
    return f"recs:page:{user_id}:v{get_user_library_version(user_id)}"

def _popular_key():
    return "recs:popular_global"
```

TTLs:

| Key                | TTL    | Notes                                              |
| ------------------ | ------ | -------------------------------------------------- |
| more_like_this     | 3600s  | version-keyed; invalidated by library mutations    |
| library_row        | 3600s  | version-keyed                                      |
| page               | 3600s  | version-keyed                                      |
| popular_global     | 21600s | shared across users; recomputed on miss            |
| _count_finished_*  | 60s    | per-user; protects cold-start branch from churn    |

`bump_user_library_version(user_id)` is already called on every library
mutation (verified during the audit). No new invalidation hooks are needed.

Cache stampede is not a concern at single-worker / few-users scale.

`?nocache=1` query param on the page and the JSON endpoints clears the
relevant key before fetching. Gated behind `@admin_required`.

## Routes

```python
recommendations_bp = Blueprint('recommendations', __name__, url_prefix='/recommendations')

@recommendations_bp.route('/', methods=['GET'])
@login_required
def page() -> Response: ...

@recommendations_bp.route('/api/more-like-this/<book_id>', methods=['GET'])
@login_required
def more_like_this(book_id: str) -> Response: ...

@recommendations_bp.route('/api/library-row', methods=['GET'])
@login_required
def library_row() -> Response: ...
```

All three are GET (no CSRF concern). On exception they return a degraded
`{"status": "error", "data": []}` with a 500 so the page can render a
"Recommendations unavailable" placeholder without breaking.

## Testing

Tests live in `tests/`, alongside the existing `test_unified_metadata.py`.

### 1. Unit tests for the scorer (no DB)

`tests/test_recommendation_scorer.py` — pure-Python tests on
`_score_candidates(signal_dicts, weights)`:

- Empty input → empty output
- Single-signal-only candidates rank lower than multi-signal
- Coreader threshold filters candidates with `<3` readers
- `recommendation_reason` correctly identifies the dominant signal

~10 cases, runs in <100ms.

### 2. Service-level tests with an in-memory Kuzu fixture

`tests/test_recommendation_service.py` — pytest fixture spins up a Kuzu DB in
a temp dir, builds a small graph (3 users, ~20 books, a couple of series, a
couple of authors), then exercises each public method:

- `get_more_like_this_sync` — books sharing author/category, anchor excluded,
  user's own library excluded
- `get_top_picks_sync` — uses last 5 finished as anchors; falls back to popular
  with <2 finished
- `get_continue_series_sync` — next unread volume per started series; empty
  when no started series
- `get_popular_sync` — orders by finished-count; user's library excluded
- Privacy: a candidate read only by 2 distinct users (below threshold) is
  omitted from the coreader signal but can still appear via content signals

The Kuzu fixture lives in `tests/conftest.py` and is reusable across future
graph-touching tests.

### 3. Route smoke tests

`tests/test_recommendation_routes.py` — Flask test client. Login a fixture
user, hit each endpoint, assert 200 + JSON shape (or 200 + HTML containing the
expected section headings).

Cases:

- 200 with data
- 200 with empty data when cold-start
- 401 / login-redirect when not authenticated

### Out of scope for v1 tests

- Cache TTL expiry (covered by `simple_cache.py`'s own concerns)
- Cypher query performance benchmarks
- Playwright end-to-end (no Playwright fixture in the repo today)

## Privacy notes

- Co-reader signal returns aggregate counts only, never user IDs.
- Threshold of `n >= 3` distinct readers prevents reverse-engineering small
  private libraries (single-user pairs never reach the surface).
- All recommendations queries are user-scoped; no cross-user output is shown
  except aggregate-popular books in the "Popular" section.
- `share_library` / `share_current_reading` flags do not need to be checked
  because we never disclose per-user reading state — only aggregate counts.

## Operational notes

- KuzuDB single-worker constraint preserved — no parallel queries, no
  background jobs.
- First-request latency on cache miss for a power user is expected to be
  noticeable (a few seconds). Mitigated by the lazy-fetch pattern on the
  card surfaces (skeleton renders immediately).
- Weights are tunable via env vars without redeploy.
- No new dependencies, no new schema, no migrations.

## Decisions log

| Question                              | Decision                                           |
| ------------------------------------- | -------------------------------------------------- |
| Surfaces in v1                        | Book detail + library row + dedicated `/recommendations` page |
| Signals                               | Content + aggregate co-reader (≥3 floor)           |
| Performance & freshness               | Real-time + 1h TTL + version-keyed invalidation    |
| Cold-start                            | Popular fallback below 2 finished books            |
| Hide books user already owns          | Yes                                                 |
| Ranker structure                      | Hybrid — shared scorer for similarity surfaces; dedicated queries for "Continue series" and "Popular" |
| Nav label for new page                | "Discover"                                          |
| More-like-this card limit             | 8 (4 visible at a time, horizontal scroll on mobile) |
| Co-reader threshold                   | ≥3 distinct readers                                 |
| Top-picks anchors                     | Last 5 finished books by `finish_date` desc        |
