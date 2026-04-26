# Graph-Based Recommendations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the "you might also enjoy" recommendations feature defined in `docs/superpowers/specs/2026-04-25-recommendations-design.md` end-to-end, with TDD on the scoring core, in-memory KuzuDB tests for the service, and Flask test-client smoke tests for the routes.

**Architecture:** New `KuzuRecommendationService` class wraps Cypher queries on the existing graph (`Book`/`Person`/`Series`/`Category`/`User` + `HAS_PERSONAL_METADATA`/`AUTHORED`/`PART_OF_SERIES`/`CATEGORIZED_AS`). Pure-Python `_score_candidates` ranker is decoupled from the DB. New `recommendation_routes` blueprint exposes one server-rendered page (`/recommendations`) and two JSON endpoints (`/recommendations/api/more-like-this/<book_id>`, `/recommendations/api/library-row`). Three UI surfaces (book detail card, library top row, dedicated page) reuse a single `_recommendation_card.html` partial.

**Tech Stack:** Python 3.13, Flask, KuzuDB (Cypher via `safe_execute_kuzu_query`), Jinja2, Bootstrap 5, vanilla JS for lazy-fetch. Pytest with a new in-memory Kuzu fixture in `tests/conftest.py`.

**File map:**

| Path | Purpose | Status |
| --- | --- | --- |
| `app/services/kuzu_recommendation_service.py` | Service class, scorer, signal queries, cold-start, caching | NEW |
| `app/routes/recommendation_routes.py` | Blueprint with 3 GET handlers | NEW |
| `app/templates/_recommendation_card.html` | Card partial reused in 3 surfaces | NEW |
| `app/templates/recommendations.html` | `/recommendations` page | NEW |
| `tests/conftest.py` | Pytest fixtures: in-memory Kuzu app + Flask test client | NEW |
| `tests/test_recommendation_scorer.py` | Pure scorer unit tests (no DB) | NEW |
| `tests/test_recommendation_service.py` | Service-level tests against in-memory Kuzu | NEW |
| `tests/test_recommendation_routes.py` | Route smoke tests (Flask test client) | NEW |
| `app/services/__init__.py` | Add `recommendation_service` lazy singleton + export | MODIFY |
| `app/routes/__init__.py` | Register `recommendations_bp` in `register_blueprints` | MODIFY |
| `app/templates/view_book_enhanced.html` | Append "More like this" section before `{% endblock content %}` | MODIFY |
| `app/templates/library.html` | Insert top row above filter bar; insert popular-fallback inside `.empty-state` | MODIFY |
| `app/templates/base.html` | Add "Discover" nav link next to "Stats" | MODIFY |

---

## Task 1: Module skeleton + weight config

**Files:**
- Create: `app/services/kuzu_recommendation_service.py`

- [ ] **Step 1: Create the module with imports, constants, and weight config**

```python
# app/services/kuzu_recommendation_service.py
"""KuzuDB-backed book recommendation service.

Computes "you might also enjoy" recommendations by combining content-based
signals (shared authors, categories, series, language) with an aggregate
co-reader signal. Designed to run on the existing single-worker KuzuDB +
simple_cache stack — no background jobs, no new schema.

See docs/superpowers/specs/2026-04-25-recommendations-design.md for the full
design rationale.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..infrastructure.kuzu_graph import safe_execute_kuzu_query
from ..utils.simple_cache import (
    MISS,
    cache_get,
    cache_set,
    get_user_library_version,
)

logger = logging.getLogger(__name__)


# Minimum number of distinct co-readers required for a co-reader signal to
# count. Floor protects single- or two-reader pairs from being identifiable
# via the recommendation aggregate.
COREADER_MIN_THRESHOLD = 3

# Number of recently-finished books used as anchors for whole-library scoring
# surfaces (top picks, library row).
TOP_PICKS_ANCHOR_COUNT = 5

# Cold-start gate: below this many finished books, every personalized surface
# falls back to popular global recommendations.
COLD_START_FINISHED_THRESHOLD = 2

# Hide a "More like this" section entirely if it produces fewer than this
# many results — a near-empty card row feels broken.
MORE_LIKE_THIS_MIN_RESULTS = 4


def _weight(name: str, default: float) -> float:
    env_key = f"RECS_WEIGHT_{name.upper()}"
    raw = os.environ.get(env_key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using default %s", env_key, raw, default)
        return default


# Tunable weights. Override at deploy time via env vars without redeploying:
#   RECS_WEIGHT_AUTHOR=6.5  RECS_WEIGHT_SERIES=10  ...
RECS_WEIGHTS: Dict[str, float] = {
    "author": _weight("author", 5.0),
    "category": _weight("category", 1.5),
    "series": _weight("series", 8.0),
    "series_next_volume": _weight("series_next_volume", 4.0),
    "coreader": _weight("coreader", 2.0),
    "language": _weight("language", 0.5),
}
```

- [ ] **Step 2: Sanity-check by importing**

Run: `python3 -c "from app.services.kuzu_recommendation_service import RECS_WEIGHTS; print(RECS_WEIGHTS)"`

Expected output (from project root, with `PYTHONPATH=.` if needed):
```
{'author': 5.0, 'category': 1.5, 'series': 8.0, 'series_next_volume': 4.0, 'coreader': 2.0, 'language': 0.5}
```

- [ ] **Step 3: Commit**

```bash
git add app/services/kuzu_recommendation_service.py
git commit -m "feat(recs): module skeleton + tunable weight config"
```

---

## Task 2: Pure scorer with TDD

**Files:**
- Modify: `app/services/kuzu_recommendation_service.py`
- Create: `tests/test_recommendation_scorer.py`

The scorer is a pure function: given per-signal counts for each candidate, return a sorted list with scores and dominant-signal reasons. No DB.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_recommendation_scorer.py
"""Unit tests for the pure recommendation scorer (no DB)."""
from app.services.kuzu_recommendation_service import (
    RECS_WEIGHTS,
    COREADER_MIN_THRESHOLD,
    score_candidates,
)


def test_empty_input_returns_empty():
    assert score_candidates({}, anchor_titles={}) == []


def test_single_signal_ranks_lower_than_multi_signal():
    # b1 has 1 author overlap; b2 has 1 author overlap + 2 categories overlap
    signals = {
        "b1": {"author": 1, "category": 0, "series": 0, "coreader": 0, "language": 0},
        "b2": {"author": 1, "category": 2, "series": 0, "coreader": 0, "language": 0},
    }
    ranked = score_candidates(signals, anchor_titles={})
    assert [c["book_id"] for c in ranked] == ["b2", "b1"]
    assert ranked[0]["score"] > ranked[1]["score"]


def test_coreader_threshold_filters_low_counts():
    signals = {
        "b1": {"author": 0, "category": 0, "series": 0, "coreader": 2, "language": 0},
        "b2": {"author": 0, "category": 0, "series": 0, "coreader": 5, "language": 0},
    }
    ranked = score_candidates(signals, anchor_titles={})
    # b1 falls below the threshold and has no other signal → dropped entirely.
    # b2 keeps the coreader signal.
    assert [c["book_id"] for c in ranked] == ["b2"]


def test_coreader_below_threshold_does_not_drop_when_other_signals_present():
    signals = {
        "b1": {"author": 1, "category": 0, "series": 0, "coreader": 2, "language": 0},
    }
    ranked = score_candidates(signals, anchor_titles={})
    assert len(ranked) == 1
    # Score from author only; coreader contribution dropped because <3.
    assert ranked[0]["score"] == RECS_WEIGHTS["author"] * 1


def test_series_signal_dominates_reason():
    signals = {
        "b1": {"author": 0, "category": 0, "series": 1, "series_next_volume": 0,
               "coreader": 0, "language": 0, "series_name": "Foundation",
               "anchor_id": "anchor1"},
    }
    ranked = score_candidates(signals, anchor_titles={"anchor1": "Foundation Book 1"})
    assert ranked[0]["recommendation_reason"] == "Same series as Foundation Book 1"


def test_series_next_volume_uses_volume_phrasing():
    signals = {
        "b1": {"author": 0, "category": 0, "series": 1, "series_next_volume": 1,
               "coreader": 0, "language": 0, "series_name": "Dune",
               "next_volume_number": 2, "anchor_id": "anchor1"},
    }
    ranked = score_candidates(signals, anchor_titles={"anchor1": "Dune"})
    assert ranked[0]["recommendation_reason"] == "Volume 2 of Dune"


def test_author_dominant_reason_uses_author_name():
    signals = {
        "b1": {"author": 1, "category": 0, "series": 0, "coreader": 0,
               "language": 0, "top_author_name": "Frank Herbert"},
    }
    ranked = score_candidates(signals, anchor_titles={})
    assert ranked[0]["recommendation_reason"] == "By Frank Herbert"


def test_category_dominant_reason_uses_category_name():
    signals = {
        "b1": {"author": 0, "category": 3, "series": 0, "coreader": 0,
               "language": 0, "top_category_name": "Science Fiction"},
    }
    ranked = score_candidates(signals, anchor_titles={})
    assert ranked[0]["recommendation_reason"] == "More Science Fiction"


def test_coreader_dominant_reason_uses_anchor_title():
    signals = {
        "b1": {"author": 0, "category": 0, "series": 0, "coreader": 50,
               "language": 0, "anchor_id": "anchor1"},
    }
    ranked = score_candidates(signals, anchor_titles={"anchor1": "Project Hail Mary"})
    assert ranked[0]["recommendation_reason"] == "Read by people who liked Project Hail Mary"


def test_language_signal_never_dominates():
    # Language alone shouldn't produce a reason; book gets dropped entirely
    # because nothing meaningful contributes.
    signals = {
        "b1": {"author": 0, "category": 0, "series": 0, "coreader": 0,
               "language": 1},
    }
    ranked = score_candidates(signals, anchor_titles={})
    # language=0.5 alone is below the meaningful-signal floor; we drop it.
    assert ranked == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_recommendation_scorer.py -v`

Expected: ImportError or AttributeError on `score_candidates` (function doesn't exist yet).

- [ ] **Step 3: Implement the scorer**

Append to `app/services/kuzu_recommendation_service.py`:

```python
# Threshold below which a candidate's total score is considered noise and
# the candidate is dropped. Equivalent to "language match alone isn't a
# real recommendation".
_MEANINGFUL_SCORE_FLOOR = 1.0


def score_candidates(
    signals: Dict[str, Dict[str, Any]],
    anchor_titles: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Rank candidate books from per-signal counts.

    ``signals`` is ``{candidate_book_id: {signal_name: value, ...meta}}``.
    Each candidate dict may also carry metadata used to build the
    recommendation_reason string:
      - ``series_name``         (str) series the candidate is in
      - ``next_volume_number``  (int) candidate's volume number when it's
                                  the immediate next unread volume
      - ``anchor_id``           (str) anchor that contributed the dominant signal
      - ``top_author_name``     (str) author with most overlap
      - ``top_category_name``   (str) category with most overlap

    ``anchor_titles`` maps anchor book ids to their titles, used to phrase
    series/coreader reasons. May be empty.

    Returns a list of dicts ``{book_id, score, recommendation_reason,
    contributions}`` sorted by score descending. Candidates whose total score
    falls below the meaningful-score floor are dropped.
    """
    weights = RECS_WEIGHTS
    out: List[Dict[str, Any]] = []
    for book_id, sig in signals.items():
        # Privacy floor on co-reader signal: contributions below the threshold
        # don't count, even if other signals are present.
        coreader_count = int(sig.get("coreader") or 0)
        coreader_effective = coreader_count if coreader_count >= COREADER_MIN_THRESHOLD else 0

        contributions = {
            "author": weights["author"] * float(sig.get("author") or 0),
            "category": weights["category"] * float(sig.get("category") or 0),
            "series": weights["series"] * float(sig.get("series") or 0),
            "series_next_volume": weights["series_next_volume"] * float(sig.get("series_next_volume") or 0),
            "coreader": weights["coreader"] * math.log(1 + coreader_effective),
            "language": weights["language"] * float(sig.get("language") or 0),
        }
        total = sum(contributions.values())
        if total < _MEANINGFUL_SCORE_FLOOR:
            continue

        reason = _dominant_reason(sig, contributions, anchor_titles)
        out.append({
            "book_id": book_id,
            "score": total,
            "recommendation_reason": reason,
            "contributions": contributions,
        })

    out.sort(key=lambda c: c["score"], reverse=True)
    return out


def _dominant_reason(
    sig: Dict[str, Any],
    contributions: Dict[str, float],
    anchor_titles: Dict[str, str],
) -> str:
    """Pick the highest-contributing signal and phrase a one-line reason.

    Language never counts as the dominant signal; if it would, fall back to
    the next-highest signal. The fallback is "Recommended for you" if no
    meaningful signal can be named (this is defensive — the score floor in
    score_candidates() should prevent us getting here in practice).
    """
    ranked = sorted(contributions.items(), key=lambda kv: kv[1], reverse=True)
    for signal_name, value in ranked:
        if value <= 0 or signal_name == "language":
            continue
        if signal_name == "series_next_volume":
            volume = sig.get("next_volume_number")
            series_name = sig.get("series_name") or "this series"
            if volume is not None:
                return f"Volume {volume} of {series_name}"
            return f"Next in {series_name}"
        if signal_name == "series":
            anchor_id = sig.get("anchor_id")
            anchor_title = anchor_titles.get(anchor_id) if anchor_id else None
            if anchor_title:
                return f"Same series as {anchor_title}"
            series_name = sig.get("series_name")
            if series_name:
                return f"Same series as {series_name}"
            return "Same series"
        if signal_name == "author":
            author_name = sig.get("top_author_name")
            if author_name:
                return f"By {author_name}"
            return "By the same author"
        if signal_name == "category":
            cat_name = sig.get("top_category_name")
            if cat_name:
                return f"More {cat_name}"
            return "Similar genre"
        if signal_name == "coreader":
            anchor_id = sig.get("anchor_id")
            anchor_title = anchor_titles.get(anchor_id) if anchor_id else None
            if anchor_title:
                return f"Read by people who liked {anchor_title}"
            return "Read by readers like you"
    return "Recommended for you"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_recommendation_scorer.py -v`

Expected: 9 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/kuzu_recommendation_service.py tests/test_recommendation_scorer.py
git commit -m "feat(recs): pure-Python scorer with reason builder + tests"
```

---

## Task 3: Pytest fixture for in-memory Kuzu graph

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/_kuzu_seed.py` (helper for graph seeding, kept out of test discovery via leading underscore)

The fixture spins up an isolated KuzuDB in a temp dir, applies the production schema via `safe_kuzu_manager` initialization, and seeds a small graph for service-level tests.

- [ ] **Step 1: Create the seed helper**

```python
# tests/_kuzu_seed.py
"""Graph seed helpers for recommendation service tests.

Builds a small consistent fixture: 3 users, 5 authors, 4 series, 6 categories,
~25 books, plus reading history that exercises every signal (shared authors,
categories, series, co-readers, language).

Intentionally NOT named ``test_*.py`` so pytest doesn't try to collect it.
"""
from __future__ import annotations
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List


def _ts(days_ago: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


def seed_graph(conn) -> Dict[str, str]:
    """Populate a fresh KuzuDB connection with deterministic test data.

    Returns a dict of named ids so tests can reference seeded entities by
    semantic name rather than UUID.
    """
    ids: Dict[str, str] = {}

    def new_id(name: str) -> str:
        ids[name] = str(uuid.uuid4())
        return ids[name]

    # Users — alice/bob/carol have reading history; newbie has none and is
    # used by cold-start tests.
    for handle in ("alice", "bob", "carol", "newbie"):
        uid = new_id(f"user_{handle}")
        conn.execute(
            "CREATE (:User {id: $id, username: $u, email: $e, "
            "share_library: false, share_current_reading: true, "
            "share_reading_activity: true, is_admin: false, "
            "created_at: $ts, updated_at: $ts})",
            {"id": uid, "u": handle, "e": f"{handle}@example.com", "ts": _ts(0)},
        )

    # Authors
    for name in ("Frank Herbert", "Isaac Asimov", "Ursula K. Le Guin",
                 "Brandon Sanderson", "Andy Weir"):
        pid = new_id(f"author_{name.split()[-1].lower()}")
        conn.execute(
            "CREATE (:Person {id: $id, name: $n, normalized_name: $nn})",
            {"id": pid, "n": name, "nn": name.lower()},
        )

    # Series
    for sname in ("Dune", "Foundation", "Earthsea", "Stormlight Archive"):
        sid = new_id(f"series_{sname.lower().replace(' ', '_')}")
        conn.execute(
            "CREATE (:Series {id: $id, name: $n, normalized_name: $nn})",
            {"id": sid, "n": sname, "nn": sname.lower()},
        )

    # Categories
    for cname in ("Science Fiction", "Fantasy", "Space Opera",
                  "Hard Science Fiction", "Epic Fantasy", "Classic"):
        cid = new_id(f"cat_{cname.lower().replace(' ', '_')}")
        conn.execute(
            "CREATE (:Category {id: $id, name: $n, normalized_name: $nn})",
            {"id": cid, "n": cname, "nn": cname.lower()},
        )

    # Books — keyed by short slug for test readability
    book_specs: List[Dict] = [
        # slug, title, language, author_slug, series_slug?, volume?, categories
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
        # AUTHORED
        conn.execute(
            "MATCH (p:Person {id: $pid}), (b:Book {id: $bid}) "
            "CREATE (p)-[:AUTHORED {role: 'authored', order_index: 0}]->(b)",
            {"pid": ids[f"author_{author_slug}"], "bid": bid},
        )
        # PART_OF_SERIES
        if series_slug:
            conn.execute(
                "MATCH (b:Book {id: $bid}), (s:Series {id: $sid}) "
                "CREATE (b)-[:PART_OF_SERIES {volume_number: $vol}]->(s)",
                {"bid": bid, "sid": ids[f"series_{series_slug}"], "vol": vol},
            )
        # CATEGORIZED_AS
        for cat in cats:
            conn.execute(
                "MATCH (b:Book {id: $bid}), (c:Category {id: $cid}) "
                "CREATE (b)-[:CATEGORIZED_AS {created_at: $ts}]->(c)",
                {"bid": bid, "cid": ids[f"cat_{cat}"], "ts": _ts(0)},
            )

    # Reading history (HAS_PERSONAL_METADATA with finish_date for "finished").
    # alice: read dune trilogy + foundation; reading earthsea
    # bob:   read dune + dune2 + foundation + foundation2 + storm1 + storm2
    # carol: read dune + foundation + hail_mary + martian
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

- [ ] **Step 2: Create the conftest with fixtures**

```python
# tests/conftest.py
"""Shared pytest fixtures for graph-touching tests.

Spins up an isolated KuzuDB per test session in a tempdir, applies the
production schema, and seeds a small deterministic graph. Tests that don't
need the graph can ignore these fixtures entirely.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from typing import Iterator, Tuple

import pytest

from tests._kuzu_seed import seed_graph


@pytest.fixture(scope="session")
def kuzu_tempdir() -> Iterator[str]:
    """Session-scoped temp dir for KuzuDB files."""
    tmp = tempfile.mkdtemp(prefix="bibliotheca_kuzu_test_")
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="session")
def kuzu_seeded(kuzu_tempdir: str) -> Iterator[Tuple[object, dict]]:
    """Session-scoped seeded KuzuDB connection.

    Returns (connection, named_ids). The connection survives the whole
    session because schema setup is heavy and the seed graph is read-only
    in tests.
    """
    # Point the safe_kuzu_manager at our temp dir BEFORE importing it.
    os.environ["KUZU_DB_PATH"] = os.path.join(kuzu_tempdir, "kuzu")
    os.environ["DATA_DIR"] = kuzu_tempdir

    # Import here so env vars take effect first.
    from app.utils.safe_kuzu_manager import get_safe_kuzu_manager

    mgr = get_safe_kuzu_manager()
    with mgr.get_connection(operation="test_seed") as conn:
        ids = seed_graph(conn)
        yield conn, ids
```

- [ ] **Step 3: Smoke-test the fixture**

Run: `pytest tests/conftest.py --collect-only -q`

Expected: no errors. (`conftest.py` itself has no tests; this just verifies imports work.)

Then run a one-off probe:

```bash
python3 -c "
import os, tempfile
tmp = tempfile.mkdtemp()
os.environ['KUZU_DB_PATH'] = os.path.join(tmp, 'kuzu')
os.environ['DATA_DIR'] = tmp
from app.utils.safe_kuzu_manager import get_safe_kuzu_manager
from tests._kuzu_seed import seed_graph
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
git add tests/conftest.py tests/_kuzu_seed.py
git commit -m "test(recs): in-memory Kuzu fixture with seeded graph"
```

---

## Task 4: Cold-start helper + cache key utilities

**Files:**
- Modify: `app/services/kuzu_recommendation_service.py`
- Modify: `tests/test_recommendation_service.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_recommendation_service.py
"""Service-level tests against the in-memory Kuzu fixture."""
import time

import pytest

from app.services.kuzu_recommendation_service import (
    KuzuRecommendationService,
    _count_finished_books,
)


@pytest.fixture
def service():
    return KuzuRecommendationService()


def test_count_finished_books_alice(kuzu_seeded):
    _, ids = kuzu_seeded
    assert _count_finished_books(ids["user_alice"]) == 4


def test_count_finished_books_unknown_user(kuzu_seeded):
    assert _count_finished_books("does-not-exist") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_recommendation_service.py -v`

Expected: ImportError on `KuzuRecommendationService` / `_count_finished_books`.

- [ ] **Step 3: Implement the helpers**

Append to `app/services/kuzu_recommendation_service.py`:

```python
# ----- Cache keys --------------------------------------------------------

_TTL_SURFACE = 3600                  # 1 hour
_TTL_POPULAR = 21600                 # 6 hours
_TTL_FINISHED_COUNT = 60             # 1 minute


def _key_more_like_this(book_id: str, user_id: str) -> str:
    return f"recs:more_like_this:{user_id}:v{get_user_library_version(user_id)}:b{book_id}"


def _key_library_row(user_id: str) -> str:
    return f"recs:library_row:{user_id}:v{get_user_library_version(user_id)}"


def _key_page(user_id: str) -> str:
    return f"recs:page:{user_id}:v{get_user_library_version(user_id)}"


def _key_popular() -> str:
    return "recs:popular_global"


def _key_finished_count(user_id: str) -> str:
    # Versioned so finishing a book invalidates instantly.
    return f"recs:finished_count:{user_id}:v{get_user_library_version(user_id)}"


# ----- Kuzu helpers -----------------------------------------------------

def _result_to_rows(result) -> List[Dict[str, Any]]:
    """Normalize the various shapes safe_execute_kuzu_query returns."""
    if result is None:
        return []
    rows: List[Dict[str, Any]] = []
    has_next = getattr(result, "has_next", None)
    get_next = getattr(result, "get_next", None)
    column_names = getattr(result, "get_column_names", lambda: None)()
    if callable(has_next) and callable(get_next):
        while result.has_next():
            row = result.get_next()
            if column_names:
                rows.append({col: row[i] for i, col in enumerate(column_names)})
            else:
                rows.append({i: v for i, v in enumerate(row)})
        return rows
    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)]
    return rows


def _count_finished_books(user_id: str) -> int:
    """Return the count of HAS_PERSONAL_METADATA edges with non-null finish_date.

    Cached for 60s per user (and version-keyed) to avoid repeating the count
    across multiple service method calls within one request.
    """
    key = _key_finished_count(user_id)
    cached = cache_get(key)
    if cached is not MISS:
        return int(cached)
    try:
        result = safe_execute_kuzu_query(
            "MATCH (u:User {id: $uid})-[m:HAS_PERSONAL_METADATA]->(:Book) "
            "WHERE m.finish_date IS NOT NULL RETURN count(*) AS n",
            {"uid": user_id},
            user_id=user_id,
            operation="recs_count_finished",
        )
        rows = _result_to_rows(result)
        n = int(rows[0].get("n", 0)) if rows else 0
    except Exception:
        logger.exception("recs: count_finished_books failed for user %s", user_id)
        n = 0
    cache_set(key, n, ttl_seconds=_TTL_FINISHED_COUNT)
    return n


# ----- Service skeleton ------------------------------------------------

class KuzuRecommendationService:
    """See module docstring. Public methods: get_more_like_this_sync,
    get_library_row_sync, get_top_picks_sync, get_continue_series_sync,
    get_popular_sync, get_recommendations_page_sync.
    """
    # Methods are added in subsequent tasks.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_recommendation_service.py -v`

Expected: 2 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/kuzu_recommendation_service.py tests/test_recommendation_service.py
git commit -m "feat(recs): cache key helpers + finished-book count"
```

---

## Task 5: Per-signal Cypher queries

**Files:**
- Modify: `app/services/kuzu_recommendation_service.py`
- Modify: `tests/test_recommendation_service.py`

Each signal helper takes anchor book IDs, returns a dict mapping candidate book ID → signal value (and any meta needed for the reason string).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_recommendation_service.py`:

```python
from app.services.kuzu_recommendation_service import (
    _signal_shared_authors,
    _signal_shared_categories,
    _signal_same_series,
    _signal_coreaders,
    _signal_language_match,
)


def test_shared_authors_includes_other_books_by_same_author(kuzu_seeded):
    _, ids = kuzu_seeded
    out = _signal_shared_authors([ids["book_dune"]])
    # Dune is by Herbert; Dune Messiah and Children of Dune share him.
    assert ids["book_dune2"] in out
    assert ids["book_dune3"] in out
    # Foundation (Asimov) does not share author.
    assert ids["book_foundation"] not in out
    # Anchor itself is excluded.
    assert ids["book_dune"] not in out


def test_shared_categories_counts_overlap(kuzu_seeded):
    _, ids = kuzu_seeded
    out = _signal_shared_categories([ids["book_dune"]])
    # dune has [sci_fi, space_opera]; dune2 has [sci_fi, space_opera] -> 2
    assert out[ids["book_dune2"]]["count"] >= 2


def test_same_series_marks_next_volume(kuzu_seeded):
    _, ids = kuzu_seeded
    out = _signal_same_series(ids["book_dune"])
    # Dune is volume 1; volume 2 (Messiah) should be flagged next.
    assert out[ids["book_dune2"]]["next_volume"] is True
    assert out[ids["book_dune2"]]["volume_number"] == 2
    # Volume 3 is in the same series but not the immediate next.
    assert out[ids["book_dune3"]]["next_volume"] is False


def test_coreaders_threshold_floor(kuzu_seeded):
    _, ids = kuzu_seeded
    # Foundation was finished by alice, bob, carol → 3 distinct readers,
    # which IS >= threshold for the raw query (filtering happens in the
    # scorer, but the query itself returns the count).
    out = _signal_coreaders([ids["book_foundation"]])
    # Books read by anyone who also read Foundation:
    assert ids["book_dune"] in out
    # Self-anchor must be excluded.
    assert ids["book_foundation"] not in out


def test_language_match(kuzu_seeded):
    _, ids = kuzu_seeded
    out = _signal_language_match([ids["book_dune"]], candidate_pool={
        ids["book_dune2"], ids["book_foundation"], ids["book_storm1"],
    })
    # All seed books are 'en' so all candidates match.
    assert out == {
        ids["book_dune2"]: True,
        ids["book_foundation"]: True,
        ids["book_storm1"]: True,
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_recommendation_service.py -v -k "signal"`

Expected: ImportError on `_signal_*` helpers.

- [ ] **Step 3: Implement the signal queries**

Append to `app/services/kuzu_recommendation_service.py`:

```python
# ----- Per-signal queries ---------------------------------------------

def _signal_shared_authors(anchors: List[str]) -> Dict[str, Dict[str, Any]]:
    """For each candidate, count distinct authors shared with any anchor.

    Also surfaces the most-overlapping author's name for use in the
    recommendation_reason.
    """
    if not anchors:
        return {}
    try:
        result = safe_execute_kuzu_query(
            "MATCH (a:Book) WHERE a.id IN $anchors "
            "MATCH (a)<-[:AUTHORED]-(p:Person)-[:AUTHORED]->(c:Book) "
            "WHERE c.id <> a.id "
            "RETURN c.id AS book_id, p.name AS author_name, count(DISTINCT a) AS overlap",
            {"anchors": list(anchors)},
            operation="recs_signal_authors",
        )
    except Exception:
        logger.exception("recs: shared_authors signal failed")
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for row in _result_to_rows(result):
        bid = row.get("book_id")
        if not bid:
            continue
        entry = out.setdefault(bid, {"count": 0, "top_author_name": None, "_top_overlap": 0})
        entry["count"] += 1
        overlap = int(row.get("overlap") or 0)
        if overlap > entry["_top_overlap"]:
            entry["_top_overlap"] = overlap
            entry["top_author_name"] = row.get("author_name")
    return out


def _signal_shared_categories(anchors: List[str]) -> Dict[str, Dict[str, Any]]:
    """For each candidate, count distinct categories shared with any anchor."""
    if not anchors:
        return {}
    try:
        result = safe_execute_kuzu_query(
            "MATCH (a:Book) WHERE a.id IN $anchors "
            "MATCH (a)-[:CATEGORIZED_AS]->(cat:Category)<-[:CATEGORIZED_AS]-(c:Book) "
            "WHERE c.id <> a.id "
            "RETURN c.id AS book_id, cat.name AS category_name, count(DISTINCT cat) AS shared",
            {"anchors": list(anchors)},
            operation="recs_signal_categories",
        )
    except Exception:
        logger.exception("recs: shared_categories signal failed")
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for row in _result_to_rows(result):
        bid = row.get("book_id")
        if not bid:
            continue
        entry = out.setdefault(bid, {"count": 0, "top_category_name": None})
        shared = int(row.get("shared") or 0)
        entry["count"] = max(entry["count"], shared)
        # Use the first category name we see as a representative label.
        if entry["top_category_name"] is None:
            entry["top_category_name"] = row.get("category_name")
    return out


def _signal_same_series(anchor_id: str) -> Dict[str, Dict[str, Any]]:
    """Single-anchor signal: candidates in the same series as the anchor.

    Marks ``next_volume = True`` when the candidate's volume_number is
    exactly anchor.volume_number + 1.
    """
    try:
        result = safe_execute_kuzu_query(
            "MATCH (a:Book {id: $anchor})-[ar:PART_OF_SERIES]->(s:Series) "
            "MATCH (s)<-[cr:PART_OF_SERIES]-(c:Book) "
            "WHERE c.id <> a.id "
            "RETURN c.id AS book_id, s.name AS series_name, "
            "       cr.volume_number AS volume_number, ar.volume_number AS anchor_volume",
            {"anchor": anchor_id},
            operation="recs_signal_series",
        )
    except Exception:
        logger.exception("recs: same_series signal failed")
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for row in _result_to_rows(result):
        bid = row.get("book_id")
        if not bid:
            continue
        cand_vol = row.get("volume_number")
        anchor_vol = row.get("anchor_volume")
        is_next = (
            cand_vol is not None
            and anchor_vol is not None
            and int(cand_vol) == int(anchor_vol) + 1
        )
        out[bid] = {
            "count": 1,
            "series_name": row.get("series_name"),
            "volume_number": int(cand_vol) if cand_vol is not None else None,
            "next_volume": is_next,
        }
    return out


def _signal_coreaders(anchors: List[str]) -> Dict[str, Dict[str, Any]]:
    """Aggregate co-reader signal.

    Returns ``{candidate_id: {count, anchor_id}}`` where ``count`` is the
    number of distinct users who finished both the anchor and the candidate.
    The privacy-floor check (``count >= COREADER_MIN_THRESHOLD``) happens in
    the scorer, not here, so the test fixture (3 readers) is observable.
    """
    if not anchors:
        return {}
    try:
        result = safe_execute_kuzu_query(
            "MATCH (a:Book) WHERE a.id IN $anchors "
            "MATCH (a)<-[m1:HAS_PERSONAL_METADATA]-(u:User)-[m2:HAS_PERSONAL_METADATA]->(c:Book) "
            "WHERE m1.finish_date IS NOT NULL AND m2.finish_date IS NOT NULL "
            "  AND c.id <> a.id "
            "RETURN c.id AS book_id, a.id AS anchor_id, count(DISTINCT u) AS n",
            {"anchors": list(anchors)},
            operation="recs_signal_coreaders",
        )
    except Exception:
        logger.exception("recs: coreaders signal failed")
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for row in _result_to_rows(result):
        bid = row.get("book_id")
        if not bid:
            continue
        n = int(row.get("n") or 0)
        existing = out.get(bid)
        if not existing or n > existing["count"]:
            out[bid] = {"count": n, "anchor_id": row.get("anchor_id")}
    return out


def _signal_language_match(anchors: List[str], candidate_pool: Iterable[str]) -> Dict[str, bool]:
    """Mark candidates whose language matches any anchor's language.

    Cheap and computed last so we only check the pool the other signals
    already produced. Returns ``{candidate_id: True}`` when there's a match;
    candidates not in the result map have no language bonus.
    """
    pool = list(set(candidate_pool))
    if not anchors or not pool:
        return {}
    try:
        result = safe_execute_kuzu_query(
            "MATCH (a:Book) WHERE a.id IN $anchors RETURN DISTINCT a.language AS lang",
            {"anchors": list(anchors)},
            operation="recs_signal_lang_anchor",
        )
        anchor_langs = {r.get("lang") for r in _result_to_rows(result) if r.get("lang")}
        if not anchor_langs:
            return {}
        result = safe_execute_kuzu_query(
            "MATCH (b:Book) WHERE b.id IN $pool RETURN b.id AS book_id, b.language AS lang",
            {"pool": pool},
            operation="recs_signal_lang_pool",
        )
        return {
            row["book_id"]: True
            for row in _result_to_rows(result)
            if row.get("book_id") and row.get("lang") in anchor_langs
        }
    except Exception:
        logger.exception("recs: language_match signal failed")
        return {}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_recommendation_service.py -v -k "signal"`

Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/kuzu_recommendation_service.py tests/test_recommendation_service.py
git commit -m "feat(recs): per-signal Cypher queries + tests"
```

---

## Task 6: Helper queries — anchors, library exclusion, book hydration, popular

**Files:**
- Modify: `app/services/kuzu_recommendation_service.py`
- Modify: `tests/test_recommendation_service.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_recommendation_service.py`:

```python
from app.services.kuzu_recommendation_service import (
    _recent_finished_anchors,
    _user_library_book_ids,
    _hydrate_books,
    _popular_global,
    _continue_series_for,
)


def test_recent_finished_anchors_orders_by_finish_date_desc(kuzu_seeded):
    _, ids = kuzu_seeded
    anchors = _recent_finished_anchors(ids["user_alice"], limit=5)
    # alice's finishes: foundation(20), dune3(30), dune2(60), dune(90)
    expected = [ids["book_foundation"], ids["book_dune3"], ids["book_dune2"], ids["book_dune"]]
    assert anchors == expected


def test_user_library_excludes_others(kuzu_seeded):
    _, ids = kuzu_seeded
    library = _user_library_book_ids(ids["user_alice"])
    assert ids["book_dune"] in library
    assert ids["book_foundation"] in library
    # Bob's books not in alice's library
    assert ids["book_storm1"] not in library


def test_hydrate_books_returns_display_fields(kuzu_seeded):
    _, ids = kuzu_seeded
    books = _hydrate_books([ids["book_dune"], ids["book_foundation"]])
    assert {b["title"] for b in books} == {"Dune", "Foundation"}
    # Authors are populated from AUTHORED edges.
    titles = {b["title"]: b for b in books}
    assert titles["Dune"]["authors"] == ["Frank Herbert"]


def test_hydrate_preserves_input_order(kuzu_seeded):
    _, ids = kuzu_seeded
    out = _hydrate_books([ids["book_foundation"], ids["book_dune"]])
    assert [b["title"] for b in out] == ["Foundation", "Dune"]


def test_popular_global_orders_by_finish_count(kuzu_seeded):
    _, ids = kuzu_seeded
    pop = _popular_global(limit=5)
    # Most finished in seed: dune (3 readers), foundation (3 readers).
    titles = [b["title"] for b in pop]
    assert "Dune" in titles[:2]
    assert "Foundation" in titles[:2]


def test_continue_series_for_alice(kuzu_seeded):
    _, ids = kuzu_seeded
    out = _continue_series_for(ids["user_alice"], limit=5)
    # alice finished foundation vol 1 only; vol 2 is the next unread.
    titles = {row["next_book"]["title"]: row for row in out}
    assert "Foundation and Empire" in titles
    # alice's most recent series finish is foundation (20 days ago) vs dune3 (30),
    # so foundation continuation should come first.
    assert out[0]["next_book"]["title"] == "Foundation and Empire"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_recommendation_service.py -v`

Expected: ImportError on the new helpers.

- [ ] **Step 3: Implement the helpers**

Append to `app/services/kuzu_recommendation_service.py`:

```python
# ----- Anchors / exclusions / hydration ----------------------------------

def _recent_finished_anchors(user_id: str, limit: int) -> List[str]:
    try:
        result = safe_execute_kuzu_query(
            "MATCH (u:User {id: $uid})-[m:HAS_PERSONAL_METADATA]->(b:Book) "
            "WHERE m.finish_date IS NOT NULL "
            "RETURN b.id AS book_id, m.finish_date AS finish_date "
            "ORDER BY m.finish_date DESC LIMIT $limit",
            {"uid": user_id, "limit": int(limit)},
            user_id=user_id,
            operation="recs_recent_finished",
        )
        return [r["book_id"] for r in _result_to_rows(result) if r.get("book_id")]
    except Exception:
        logger.exception("recs: recent_finished_anchors failed")
        return []


def _user_library_book_ids(user_id: str) -> set:
    try:
        result = safe_execute_kuzu_query(
            "MATCH (u:User {id: $uid})-[:HAS_PERSONAL_METADATA]->(b:Book) "
            "RETURN b.id AS book_id",
            {"uid": user_id},
            user_id=user_id,
            operation="recs_user_library_ids",
        )
        return {r["book_id"] for r in _result_to_rows(result) if r.get("book_id")}
    except Exception:
        logger.exception("recs: user_library_book_ids failed")
        return set()


def _hydrate_books(book_ids: List[str]) -> List[Dict[str, Any]]:
    """Fetch display fields for an ordered list of book ids.

    Returns dicts in the same order as ``book_ids`` (missing ids are
    silently dropped). Includes title, isbn13/10, cover_url, language, and
    authors derived from AUTHORED edges.
    """
    if not book_ids:
        return []
    by_id: Dict[str, Dict[str, Any]] = {}
    try:
        result = safe_execute_kuzu_query(
            "MATCH (b:Book) WHERE b.id IN $ids "
            "OPTIONAL MATCH (p:Person)-[:AUTHORED]->(b) "
            "RETURN b.id AS id, b.title AS title, b.isbn13 AS isbn13, "
            "       b.isbn10 AS isbn10, b.cover_url AS cover_url, "
            "       b.language AS language, p.name AS author_name",
            {"ids": list(book_ids)},
            operation="recs_hydrate_books",
        )
        for row in _result_to_rows(result):
            bid = row.get("id")
            if not bid:
                continue
            entry = by_id.setdefault(bid, {
                "id": bid,
                "title": row.get("title") or "",
                "isbn13": row.get("isbn13"),
                "isbn10": row.get("isbn10"),
                "cover_url": row.get("cover_url"),
                "language": row.get("language"),
                "authors": [],
            })
            author = row.get("author_name")
            if author and author not in entry["authors"]:
                entry["authors"].append(author)
    except Exception:
        logger.exception("recs: _hydrate_books failed")
        return []
    return [by_id[bid] for bid in book_ids if bid in by_id]


def _popular_global(limit: int = 50) -> List[Dict[str, Any]]:
    """Top books by distinct-user finish count (cached cross-user)."""
    cached = cache_get(_key_popular())
    if cached is not MISS:
        return cached[:limit]
    try:
        result = safe_execute_kuzu_query(
            "MATCH (b:Book)<-[m:HAS_PERSONAL_METADATA]-(u:User) "
            "WHERE m.finish_date IS NOT NULL "
            "WITH b.id AS book_id, count(DISTINCT u) AS n "
            "ORDER BY n DESC LIMIT 50 "
            "RETURN book_id",
            None,
            operation="recs_popular_global",
        )
        ids = [r["book_id"] for r in _result_to_rows(result) if r.get("book_id")]
        books = _hydrate_books(ids)
        for b in books:
            b["recommendation_reason"] = "Popular among readers"
        cache_set(_key_popular(), books, ttl_seconds=_TTL_POPULAR)
        return books[:limit]
    except Exception:
        logger.exception("recs: popular_global failed")
        return []


def _continue_series_for(user_id: str, limit: int) -> List[Dict[str, Any]]:
    """Return one card per started-but-incomplete series."""
    try:
        # Step A — started series and recent-finish timestamps.
        result = safe_execute_kuzu_query(
            "MATCH (u:User {id: $uid})-[m:HAS_PERSONAL_METADATA]->"
            "(:Book)-[:PART_OF_SERIES]->(s:Series) "
            "WHERE m.finish_date IS NOT NULL "
            "RETURN s.id AS series_id, s.name AS series_name, "
            "       max(m.finish_date) AS recent_finish",
            {"uid": user_id},
            user_id=user_id,
            operation="recs_continue_series_a",
        )
        series_rows = _result_to_rows(result)
        # Sort series by recency before fetching next-volumes.
        series_rows.sort(key=lambda r: r.get("recent_finish") or 0, reverse=True)
        out: List[Dict[str, Any]] = []
        for row in series_rows:
            sid = row.get("series_id")
            sname = row.get("series_name") or "this series"
            if not sid:
                continue
            # Step B — lowest unread volume for this series, for this user.
            res2 = safe_execute_kuzu_query(
                "MATCH (s:Series {id: $sid})<-[r:PART_OF_SERIES]-(next:Book) "
                "WHERE NOT EXISTS { "
                "  MATCH (:User {id: $uid})-[:HAS_PERSONAL_METADATA]->(next) "
                "} "
                "RETURN next.id AS book_id, r.volume_number AS volume_number "
                "ORDER BY r.volume_number ASC LIMIT 1",
                {"sid": sid, "uid": user_id},
                user_id=user_id,
                operation="recs_continue_series_b",
            )
            next_rows = _result_to_rows(res2)
            if not next_rows:
                continue
            nxt = next_rows[0]
            books = _hydrate_books([nxt["book_id"]])
            if not books:
                continue
            book = books[0]
            volume_number = nxt.get("volume_number")
            book["recommendation_reason"] = (
                f"Volume {volume_number} of {sname}" if volume_number is not None
                else f"Next in {sname}"
            )
            out.append({"series_name": sname, "next_book": book})
            if len(out) >= limit:
                break
        return out
    except Exception:
        logger.exception("recs: continue_series_for failed")
        return []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_recommendation_service.py -v`

Expected: 12 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/kuzu_recommendation_service.py tests/test_recommendation_service.py
git commit -m "feat(recs): anchor, library, hydration, popular, continue-series helpers"
```

---

## Task 7: Service public methods — more_like_this, library_row, top_picks, popular, continue_series

**Files:**
- Modify: `app/services/kuzu_recommendation_service.py`
- Modify: `tests/test_recommendation_service.py`

These methods are thin: assemble signals → score → filter → hydrate → cache.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_recommendation_service.py`:

```python
def test_more_like_this_excludes_anchor_and_user_library(kuzu_seeded, service):
    _, ids = kuzu_seeded
    out = service.get_more_like_this_sync(
        book_id=ids["book_dune"], user_id=ids["user_alice"], limit=10,
    )
    titles = [b["title"] for b in out]
    # alice already finished Dune 1/2/3 + Foundation; none of those should appear.
    assert "Dune" not in titles
    assert "Dune Messiah" not in titles
    assert "Foundation" not in titles
    # Each result has a reason string.
    for b in out:
        assert b["recommendation_reason"]


def test_more_like_this_for_bob_surfaces_dune3(kuzu_seeded, service):
    _, ids = kuzu_seeded
    # bob has read dune + dune2 + foundations + storm; dune3 is NOT in his library.
    out = service.get_more_like_this_sync(
        book_id=ids["book_dune"], user_id=ids["user_bob"], limit=10,
    )
    titles = [b["title"] for b in out]
    assert "Children of Dune" in titles


def test_top_picks_falls_back_to_popular_for_cold_user(kuzu_seeded, service):
    _, ids = kuzu_seeded
    # 'newbie' is seeded with no finishes (see _kuzu_seed.py).
    out = service.get_top_picks_sync(user_id=ids["user_newbie"], limit=10)
    # All of them are popular-fallback.
    assert all(b["recommendation_reason"] == "Popular among readers" for b in out)


def test_top_picks_personalized_for_alice(kuzu_seeded, service):
    _, ids = kuzu_seeded
    out = service.get_top_picks_sync(user_id=ids["user_alice"], limit=10)
    # Should include at least one non-popular reason (real signal-driven rec).
    reasons = {b["recommendation_reason"] for b in out}
    assert any(r != "Popular among readers" for r in reasons)


def test_continue_series_sync_returns_books(kuzu_seeded, service):
    _, ids = kuzu_seeded
    out = service.get_continue_series_sync(user_id=ids["user_alice"], limit=5)
    titles = [b["title"] for b in out]
    assert "Foundation and Empire" in titles
    # Continue-series cards always carry the volume reason.
    for b in out:
        assert "Volume" in b["recommendation_reason"] or "Next in" in b["recommendation_reason"]


def test_popular_sync_excludes_user_library(kuzu_seeded, service):
    _, ids = kuzu_seeded
    out = service.get_popular_sync(user_id=ids["user_alice"], limit=20)
    titles = [b["title"] for b in out]
    assert "Dune" not in titles  # alice already has it
    assert "Foundation" not in titles
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_recommendation_service.py -v -k "more_like_this or top_picks or continue_series_sync or popular_sync"`

Expected: AttributeError — methods don't exist.

- [ ] **Step 3: Implement the public methods**

Append to `KuzuRecommendationService` class body:

```python
    # --- internal: shared scoring pipeline ---

    def _score_for_anchors(
        self,
        anchors: List[str],
        anchor_titles: Dict[str, str],
        user_id: str,
        limit: int,
        single_anchor_series: bool,
    ) -> List[Dict[str, Any]]:
        if not anchors:
            return []
        author_sig = _signal_shared_authors(anchors)
        category_sig = _signal_shared_categories(anchors)
        if single_anchor_series:
            series_sig = _signal_same_series(anchors[0])
        else:
            series_sig = {}
        coreader_sig = _signal_coreaders(anchors)

        candidate_ids = (
            set(author_sig)
            | set(category_sig)
            | set(series_sig)
            | set(coreader_sig)
        )
        # Drop the anchors themselves and books the user already owns.
        candidate_ids.difference_update(anchors)
        candidate_ids.difference_update(_user_library_book_ids(user_id))
        if not candidate_ids:
            return []

        lang_sig = _signal_language_match(anchors, candidate_ids)

        # Build the merged signal map the scorer consumes.
        merged: Dict[str, Dict[str, Any]] = {}
        for cid in candidate_ids:
            a = author_sig.get(cid, {})
            c = category_sig.get(cid, {})
            s = series_sig.get(cid, {})
            cr = coreader_sig.get(cid, {})
            merged[cid] = {
                "author": a.get("count", 0),
                "top_author_name": a.get("top_author_name"),
                "category": c.get("count", 0),
                "top_category_name": c.get("top_category_name"),
                "series": s.get("count", 0),
                "series_name": s.get("series_name"),
                "next_volume_number": s.get("volume_number"),
                "series_next_volume": 1 if s.get("next_volume") else 0,
                "coreader": cr.get("count", 0),
                "anchor_id": cr.get("anchor_id") or (anchors[0] if anchors else None),
                "language": 1 if lang_sig.get(cid) else 0,
            }
        ranked = score_candidates(merged, anchor_titles)
        ranked = ranked[:limit]
        ordered_ids = [r["book_id"] for r in ranked]
        books = _hydrate_books(ordered_ids)
        # Stitch reason + score onto each hydrated book by id.
        rank_by_id = {r["book_id"]: r for r in ranked}
        for b in books:
            r = rank_by_id.get(b["id"], {})
            b["recommendation_reason"] = r.get("recommendation_reason", "Recommended for you")
            b["score"] = r.get("score")
        return books

    # --- public surfaces ---

    def get_more_like_this_sync(
        self, book_id: str, user_id: str, limit: int = 8,
    ) -> List[Dict[str, Any]]:
        if not book_id or not user_id:
            return []
        key = _key_more_like_this(book_id, user_id)
        cached = cache_get(key)
        if cached is not MISS:
            return cached[:limit]
        anchor_titles = self._anchor_titles([book_id])
        results = self._score_for_anchors(
            anchors=[book_id],
            anchor_titles=anchor_titles,
            user_id=user_id,
            limit=limit,
            single_anchor_series=True,
        )
        cache_set(key, results, ttl_seconds=_TTL_SURFACE)
        return results

    def get_library_row_sync(self, user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        if not user_id:
            return []
        key = _key_library_row(user_id)
        cached = cache_get(key)
        if cached is not MISS:
            return cached[:limit]
        if _count_finished_books(user_id) < COLD_START_FINISHED_THRESHOLD:
            results = self.get_popular_sync(user_id, limit=limit)
        else:
            anchors = _recent_finished_anchors(user_id, TOP_PICKS_ANCHOR_COUNT)
            anchor_titles = self._anchor_titles(anchors)
            results = self._score_for_anchors(
                anchors=anchors,
                anchor_titles=anchor_titles,
                user_id=user_id,
                limit=limit,
                single_anchor_series=False,
            )
            if not results:
                results = self.get_popular_sync(user_id, limit=limit)
        cache_set(key, results, ttl_seconds=_TTL_SURFACE)
        return results

    def get_top_picks_sync(self, user_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        if not user_id:
            return []
        if _count_finished_books(user_id) < COLD_START_FINISHED_THRESHOLD:
            return self.get_popular_sync(user_id, limit=limit)
        anchors = _recent_finished_anchors(user_id, TOP_PICKS_ANCHOR_COUNT)
        anchor_titles = self._anchor_titles(anchors)
        results = self._score_for_anchors(
            anchors=anchors,
            anchor_titles=anchor_titles,
            user_id=user_id,
            limit=limit,
            single_anchor_series=False,
        )
        if not results:
            results = self.get_popular_sync(user_id, limit=limit)
        return results

    def get_continue_series_sync(self, user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        if not user_id:
            return []
        rows = _continue_series_for(user_id, limit=limit)
        return [r["next_book"] for r in rows]

    def get_popular_sync(self, user_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        all_pop = _popular_global(limit=50)
        owned = _user_library_book_ids(user_id) if user_id else set()
        return [b for b in all_pop if b["id"] not in owned][:limit]

    # --- helpers ---

    def _anchor_titles(self, anchors: List[str]) -> Dict[str, str]:
        if not anchors:
            return {}
        try:
            result = safe_execute_kuzu_query(
                "MATCH (b:Book) WHERE b.id IN $ids RETURN b.id AS id, b.title AS title",
                {"ids": list(anchors)},
                operation="recs_anchor_titles",
            )
            return {r["id"]: r.get("title", "") for r in _result_to_rows(result) if r.get("id")}
        except Exception:
            logger.exception("recs: _anchor_titles failed")
            return {}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_recommendation_service.py -v`

Expected: all 18 service tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/kuzu_recommendation_service.py tests/test_recommendation_service.py
git commit -m "feat(recs): public service methods for all 5 surfaces"
```

---

## Task 8: Composite page bundle + service singleton

**Files:**
- Modify: `app/services/kuzu_recommendation_service.py`
- Modify: `app/services/__init__.py`
- Modify: `tests/test_recommendation_service.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_recommendation_service.py`:

```python
def test_recommendations_page_bundle_for_alice(kuzu_seeded, service):
    _, ids = kuzu_seeded
    bundle = service.get_recommendations_page_sync(ids["user_alice"])
    assert set(bundle.keys()) == {"top_picks", "continue_series", "popular", "personalized"}
    assert bundle["personalized"] is True
    assert isinstance(bundle["top_picks"], list)
    assert isinstance(bundle["continue_series"], list)
    assert isinstance(bundle["popular"], list)


def test_recommendations_page_bundle_cold_start(kuzu_seeded, service):
    _, ids = kuzu_seeded
    bundle = service.get_recommendations_page_sync(ids["user_newbie"])
    assert bundle["personalized"] is False
    # continue_series is gated behind personalized=True, so always empty here.
    assert bundle["continue_series"] == []
    # top_picks falls back to popular; both should have data from seed.
    assert all(k in bundle for k in ("top_picks", "continue_series", "popular"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_recommendation_service.py -v -k "recommendations_page_bundle"`

Expected: AttributeError on `get_recommendations_page_sync`.

- [ ] **Step 3: Implement the composite method**

Append to `KuzuRecommendationService`:

```python
    def get_recommendations_page_sync(self, user_id: str) -> Dict[str, Any]:
        if not user_id:
            return {"top_picks": [], "continue_series": [], "popular": [], "personalized": False}
        key = _key_page(user_id)
        cached = cache_get(key)
        if cached is not MISS:
            return cached
        finished = _count_finished_books(user_id)
        personalized = finished >= COLD_START_FINISHED_THRESHOLD
        bundle = {
            "top_picks": self.get_top_picks_sync(user_id, limit=20),
            "continue_series": self.get_continue_series_sync(user_id, limit=10) if personalized else [],
            "popular": self.get_popular_sync(user_id, limit=20),
            "personalized": personalized,
        }
        cache_set(key, bundle, ttl_seconds=_TTL_SURFACE)
        return bundle
```

- [ ] **Step 4: Wire the lazy singleton**

In `app/services/__init__.py`, alongside the existing `_get_*_service` helpers:

```python
    # Add near the other lazy-getter functions (around line 99, after
    # _get_reading_log_service):
    _recommendation_service = None

    def _get_recommendation_service():
        global _recommendation_service
        if _recommendation_service is None:
            _run_migration_once()
            from .kuzu_recommendation_service import KuzuRecommendationService
            _recommendation_service = KuzuRecommendationService()
        return _recommendation_service
```

In the section where lazy instances are created (around the existing
`book_service = _LazyService(_get_book_service)` line), add:

```python
    recommendation_service = _LazyService(_get_recommendation_service)
```

In the `reset_all_services()` function, alongside the other resets, add:

```python
        global _recommendation_service
        global recommendation_service
        _recommendation_service = None
        if hasattr(recommendation_service, "_service"):
            recommendation_service._service = None  # type: ignore[attr-defined]
```

And after the wrapper recreation block, add:

```python
        recommendation_service = _LazyService(_get_recommendation_service)
```

In the `__all__` list, add `'recommendation_service'`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_recommendation_service.py -v`

Then verify the singleton wires up:

```bash
python3 -c "from app.services import recommendation_service; print(type(recommendation_service))"
```

Expected: `<class 'app.services.LazyService'>` (or similar; not an ImportError).

- [ ] **Step 6: Commit**

```bash
git add app/services/kuzu_recommendation_service.py app/services/__init__.py tests/test_recommendation_service.py
git commit -m "feat(recs): page bundle + lazy service singleton"
```

---

## Task 9: Blueprint with three GET handlers + route smoke tests

**Files:**
- Create: `app/routes/recommendation_routes.py`
- Modify: `app/routes/__init__.py`
- Create: `tests/test_recommendation_routes.py`

- [ ] **Step 1: Write the route file**

```python
# app/routes/recommendation_routes.py
"""Recommendations blueprint.

Exposes one server-rendered page (/recommendations) and two JSON endpoints
the existing book-detail and library pages lazy-fetch from.
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, render_template, request
from flask_login import current_user, login_required

from ..services import recommendation_service

logger = logging.getLogger(__name__)

recommendations_bp = Blueprint(
    "recommendations", __name__, url_prefix="/recommendations"
)


def _user_id() -> str:
    return str(getattr(current_user, "id", ""))


@recommendations_bp.route("/", methods=["GET"])
@login_required
def page():
    """Server-rendered /recommendations dashboard."""
    try:
        bundle = recommendation_service.get_recommendations_page_sync(_user_id())
    except Exception:
        logger.exception("recommendations.page failed")
        bundle = {"top_picks": [], "continue_series": [], "popular": [], "personalized": False}
    return render_template("recommendations.html", **bundle)


@recommendations_bp.route("/api/more-like-this/<book_id>", methods=["GET"])
@login_required
def more_like_this(book_id: str):
    limit = request.args.get("limit", default=8, type=int) or 8
    limit = max(1, min(int(limit), 20))
    try:
        data = recommendation_service.get_more_like_this_sync(
            book_id=str(book_id), user_id=_user_id(), limit=limit,
        )
        return jsonify({"status": "success", "data": data, "count": len(data)})
    except Exception:
        logger.exception("recommendations.more_like_this failed for %s", book_id)
        return jsonify({"status": "error", "data": [], "count": 0}), 500


@recommendations_bp.route("/api/library-row", methods=["GET"])
@login_required
def library_row():
    limit = request.args.get("limit", default=10, type=int) or 10
    limit = max(1, min(int(limit), 30))
    try:
        data = recommendation_service.get_library_row_sync(
            user_id=_user_id(), limit=limit,
        )
        return jsonify({"status": "success", "data": data, "count": len(data)})
    except Exception:
        logger.exception("recommendations.library_row failed")
        return jsonify({"status": "error", "data": [], "count": 0}), 500
```

- [ ] **Step 2: Register the blueprint**

In `app/routes/__init__.py`, add the import near the top (alongside the
others) and register inside `register_blueprints`:

```python
# Add to imports near top of file:
from .recommendation_routes import recommendations_bp

# Inside register_blueprints(), after the reading_logs registration:
    app.register_blueprint(recommendations_bp)
```

Add `'recommendations_bp'` to the module `__all__` list at the bottom.

- [ ] **Step 3: Write route smoke tests**

```python
# tests/test_recommendation_routes.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_recommendation_routes.py -v`

Expected: 4 tests pass. (If `_login` needs adjustments to match the project's auth helpers, see existing tests; the goal is a logged-in session.)

- [ ] **Step 5: Commit**

```bash
git add app/routes/recommendation_routes.py app/routes/__init__.py tests/test_recommendation_routes.py
git commit -m "feat(recs): /recommendations blueprint with 3 GET endpoints"
```

---

## Task 10: Card partial template

**Files:**
- Create: `app/templates/_recommendation_card.html`

- [ ] **Step 1: Write the partial**

```jinja
{# app/templates/_recommendation_card.html
   Reusable recommendation card. Caller passes:
     - book: dict with id, title, authors (list), cover_url, recommendation_reason
     - show_reason: bool (default True) — hide on the library row for cleanliness
#}
{% set show_reason = show_reason if show_reason is defined else True %}
{% set author = (book.authors[0] if book.authors and book.authors|length > 0 else "") %}
<a class="recommendation-card" href="{{ url_for('book.view_book_enhanced', uid=book.id) }}"
   aria-label="Recommendation: {{ book.title|e }}{% if author %} by {{ author|e }}{% endif %}{% if book.recommendation_reason %}. {{ book.recommendation_reason|e }}{% endif %}">
  <div class="recommendation-card-cover">
    <img src="{{ book.cover_url or '/static/bookshelf.png' }}"
         alt="" loading="lazy"
         onerror="this.src='/static/bookshelf.png'">
  </div>
  <div class="recommendation-card-body">
    <div class="recommendation-card-title">{{ book.title }}</div>
    {% if author %}
    <div class="recommendation-card-author">{{ author }}</div>
    {% endif %}
    {% if show_reason and book.recommendation_reason %}
    <div class="recommendation-card-reason text-muted small">{{ book.recommendation_reason }}</div>
    {% endif %}
  </div>
</a>
```

- [ ] **Step 2: Sanity-check the partial loads in the test client**

This will be exercised by `test_page_renders_for_alice` in the next task; for
now, just verify Jinja can parse it:

```bash
python3 -c "
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('app/templates'))
env.get_template('_recommendation_card.html')
print('ok')
"
```

Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add app/templates/_recommendation_card.html
git commit -m "feat(recs): reusable recommendation card partial"
```

---

## Task 11: /recommendations page template

**Files:**
- Create: `app/templates/recommendations.html`

- [ ] **Step 1: Write the page template**

```jinja
{% extends "base.html" %}

{% block title %}Discover - MyBibliotheca{% endblock %}

{% block content %}
<style>
.recommendation-row {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 1rem;
}
@media (max-width: 768px) {
  .recommendation-row {
    display: flex;
    overflow-x: auto;
    scroll-snap-type: x mandatory;
    gap: 0.75rem;
  }
  .recommendation-row > * { scroll-snap-align: start; flex: 0 0 45%; }
}
.recommendation-card {
  display: block;
  text-decoration: none;
  color: inherit;
  border-radius: 8px;
  overflow: hidden;
  background: var(--surface-secondary, #fff);
  border: 1px solid var(--border-soft, rgba(0,0,0,.08));
  transition: transform 120ms ease, box-shadow 120ms ease;
}
.recommendation-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 18px rgba(0,0,0,.08);
}
.recommendation-card-cover img {
  width: 100%; aspect-ratio: 2/3; object-fit: cover; display: block;
}
.recommendation-card-body { padding: 0.5rem 0.75rem 0.75rem; }
.recommendation-card-title { font-weight: 600; font-size: 0.95rem; line-height: 1.2; }
.recommendation-card-author { font-size: 0.85rem; color: var(--text-color, #555); margin-top: 2px; }
.recommendation-card-reason { font-size: 0.78rem; margin-top: 4px; }
</style>

<div class="container">
  <h1 class="mb-3">Discover</h1>

  {% if not personalized %}
  <div class="alert alert-info" role="status">
    Finish a couple of books to personalize this. Until then, here's what other
    readers are loving.
  </div>
  {% endif %}

  <section class="mb-4">
    <h2 class="h4 mb-3">
      {% if personalized %}Top picks for you{% else %}Popular among readers{% endif %}
    </h2>
    {% if top_picks %}
      <div class="recommendation-row">
        {% for book in top_picks %}
          {% include "_recommendation_card.html" %}
        {% endfor %}
      </div>
    {% else %}
      <p class="text-muted">No recommendations yet — try adding a few books to your library.</p>
    {% endif %}
  </section>

  {% if continue_series %}
  <section class="mb-4">
    <h2 class="h4 mb-3">Continue your series</h2>
    <div class="recommendation-row">
      {% for book in continue_series %}
        {% include "_recommendation_card.html" %}
      {% endfor %}
    </div>
  </section>
  {% endif %}

  <section class="mb-4">
    <h2 class="h4 mb-3">Popular</h2>
    {% if popular %}
      <div class="recommendation-row">
        {% for book in popular %}
          {% with show_reason=False %}
            {% include "_recommendation_card.html" %}
          {% endwith %}
        {% endfor %}
      </div>
    {% else %}
      <p class="text-muted">Nothing to show yet.</p>
    {% endif %}
  </section>
</div>
{% endblock %}
```

- [ ] **Step 2: Verify the route now renders**

Run: `pytest tests/test_recommendation_routes.py::test_page_renders_for_alice -v`

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add app/templates/recommendations.html
git commit -m "feat(recs): /recommendations page template"
```

---

## Task 12: "More like this" on the book detail page

**Files:**
- Modify: `app/templates/view_book_enhanced.html`

The book detail file is huge (~5k lines) and ends with `{% block scripts %}`
on line 5029 then `{% endblock %}{% endblock %}`. The card section goes inside
the existing content block, before scripts, and the JS goes inside the
existing scripts block.

- [ ] **Step 1: Insert the card section before the closing of `{% block content %}`**

Read the file around lines 5020-5048 (before scripts block) to confirm structure:

```bash
sed -n '5020,5050p' app/templates/view_book_enhanced.html
```

Insert this Jinja just before the line that closes the content block (the
first `{% endblock %}` near the end — line 5048 in current state, but locate
it by pattern not by number):

```jinja

  {# --- More like this --- #}
  <section id="more-like-this-section" class="mt-4" hidden>
    <h3 class="h5 mb-3">More like this</h3>
    <div id="more-like-this-row" class="recommendation-row" aria-busy="true">
      {# Skeletons; replaced by JS on fetch #}
      {% for _ in range(4) %}
      <div class="recommendation-card" aria-hidden="true">
        <div class="recommendation-card-cover" style="background:#eee;aspect-ratio:2/3;"></div>
        <div class="recommendation-card-body">
          <div class="recommendation-card-title">&nbsp;</div>
          <div class="recommendation-card-author">&nbsp;</div>
        </div>
      </div>
      {% endfor %}
    </div>
  </section>
```

- [ ] **Step 2: Add the lazy-fetch JS to the scripts block**

Append inside the existing `{% block scripts %}` ... `{% endblock %}` near the
end of the file (line 5029 area):

```html
<script>
(function(){
  const section = document.getElementById('more-like-this-section');
  const row = document.getElementById('more-like-this-row');
  if (!section || !row) return;
  const bookId = {{ book.id|tojson }};
  if (!bookId) return;
  fetch('/recommendations/api/more-like-this/' + encodeURIComponent(bookId), {
    credentials: 'same-origin',
    headers: { 'Accept': 'application/json' }
  })
    .then(r => r.json())
    .then(payload => {
      const data = (payload && payload.data) || [];
      // Hide entirely if fewer than 4 results — a near-empty section feels broken.
      if (data.length < 4) { section.remove(); return; }
      row.innerHTML = '';
      row.removeAttribute('aria-busy');
      for (const book of data) {
        const a = document.createElement('a');
        a.className = 'recommendation-card';
        a.href = '/book/' + encodeURIComponent(book.id);
        const author = (book.authors && book.authors[0]) || '';
        a.setAttribute('aria-label',
          'Recommendation: ' + (book.title || '') +
          (author ? ' by ' + author : '') +
          (book.recommendation_reason ? '. ' + book.recommendation_reason : ''));
        const cover = document.createElement('div');
        cover.className = 'recommendation-card-cover';
        const img = document.createElement('img');
        img.alt = '';
        img.loading = 'lazy';
        img.src = book.cover_url || '/static/bookshelf.png';
        img.onerror = function(){ img.src = '/static/bookshelf.png'; };
        cover.appendChild(img);
        const body = document.createElement('div');
        body.className = 'recommendation-card-body';
        const title = document.createElement('div');
        title.className = 'recommendation-card-title';
        title.textContent = book.title || '';
        body.appendChild(title);
        if (author) {
          const au = document.createElement('div');
          au.className = 'recommendation-card-author';
          au.textContent = author;
          body.appendChild(au);
        }
        if (book.recommendation_reason) {
          const reason = document.createElement('div');
          reason.className = 'recommendation-card-reason text-muted small';
          reason.textContent = book.recommendation_reason;
          body.appendChild(reason);
        }
        a.appendChild(cover);
        a.appendChild(body);
        row.appendChild(a);
      }
      section.hidden = false;
    })
    .catch(err => {
      console.warn('More like this failed:', err);
      section.remove();
    });
})();
</script>
```

- [ ] **Step 3: Manual smoke test in the dev container**

```bash
docker cp app/templates/view_book_enhanced.html mybibliotheca-bibliotheca-1:/app/app/templates/view_book_enhanced.html
docker compose -f docker-compose.dev.yml restart bibliotheca
```

Open http://localhost:5054/ → log in → open any book → confirm the "More
like this" section appears with cards (or is hidden if <4 results).

- [ ] **Step 4: Commit**

```bash
git add app/templates/view_book_enhanced.html
git commit -m "feat(recs): More-like-this card on book detail page"
```

---

## Task 13: Library top row + empty-state seeding

**Files:**
- Modify: `app/templates/library.html`

- [ ] **Step 1: Find the insertion points**

```bash
grep -n "filter-bar\|empty-state\|book-list-container\|<h1\|<h2" app/templates/library.html | head -20
```

The top row goes above the filter bar. The empty-state seed goes inside the
existing `<div class="empty-state">` block (around line 503), after the
existing message.

- [ ] **Step 2: Insert the top-row container above the filter bar**

Add this block immediately above the `filter-bar` opening tag (locate it via
grep — do not rely on line numbers):

```jinja
{# Recommended-for-you row, hidden when filtering or searching #}
{% set _has_filter = (request.args.get('search') or request.args.get('status_filter') or request.args.get('category') or request.args.get('publisher') or request.args.get('language') or request.args.get('location') or request.args.get('media_type') or request.args.get('finished_after') or request.args.get('finished_before')) %}
{% if not _has_filter and stats and stats.books_read and stats.books_read > 0 %}
<section id="library-recs-section" class="mb-3" hidden>
  <h2 class="h5 mb-2">Recommended for you</h2>
  <div id="library-recs-row" class="recommendation-row" aria-busy="true">
    {% for _ in range(5) %}
    <div class="recommendation-card" aria-hidden="true">
      <div class="recommendation-card-cover" style="background:#eee;aspect-ratio:2/3;"></div>
      <div class="recommendation-card-body">
        <div class="recommendation-card-title">&nbsp;</div>
      </div>
    </div>
    {% endfor %}
  </div>
</section>
{% endif %}
```

- [ ] **Step 3: Insert the empty-state popular seed**

Inside the existing `<div class="empty-state">` block, immediately after the
existing "No books found" content (locate by `grep -n 'empty-state'` — usually
line 503), append:

```jinja

  {# Popular fallback to seed a brand-new library #}
  <section id="library-empty-recs-section" class="mt-4" hidden>
    <h3 class="h6">Popular books to start your library</h3>
    <div id="library-empty-recs-row" class="recommendation-row" aria-busy="true">
      {% for _ in range(5) %}
      <div class="recommendation-card" aria-hidden="true">
        <div class="recommendation-card-cover" style="background:#eee;aspect-ratio:2/3;"></div>
        <div class="recommendation-card-body">
          <div class="recommendation-card-title">&nbsp;</div>
        </div>
      </div>
      {% endfor %}
    </div>
  </section>
```

- [ ] **Step 4: Append the lazy-fetch JS**

Append at the bottom of `library.html` (end of file or in a `{% block scripts %}` if the template has one). The same fetcher reused for both rows:

```html
<script>
(function(){
  function renderInto(row, books, hideReason) {
    row.innerHTML = '';
    row.removeAttribute('aria-busy');
    for (const book of books) {
      const a = document.createElement('a');
      a.className = 'recommendation-card';
      a.href = '/book/' + encodeURIComponent(book.id);
      const author = (book.authors && book.authors[0]) || '';
      a.setAttribute('aria-label',
        'Recommendation: ' + (book.title || '') +
        (author ? ' by ' + author : '') +
        (!hideReason && book.recommendation_reason ? '. ' + book.recommendation_reason : ''));
      const cover = document.createElement('div');
      cover.className = 'recommendation-card-cover';
      const img = document.createElement('img');
      img.alt = '';
      img.loading = 'lazy';
      img.src = book.cover_url || '/static/bookshelf.png';
      img.onerror = function(){ img.src = '/static/bookshelf.png'; };
      cover.appendChild(img);
      const body = document.createElement('div');
      body.className = 'recommendation-card-body';
      const title = document.createElement('div');
      title.className = 'recommendation-card-title';
      title.textContent = book.title || '';
      body.appendChild(title);
      if (author) {
        const au = document.createElement('div');
        au.className = 'recommendation-card-author';
        au.textContent = author;
        body.appendChild(au);
      }
      if (!hideReason && book.recommendation_reason) {
        const reason = document.createElement('div');
        reason.className = 'recommendation-card-reason text-muted small';
        reason.textContent = book.recommendation_reason;
        body.appendChild(reason);
      }
      a.appendChild(cover);
      a.appendChild(body);
      row.appendChild(a);
    }
  }
  function loadInto(sectionId, rowId, hideReason) {
    const section = document.getElementById(sectionId);
    const row = document.getElementById(rowId);
    if (!section || !row) return;
    fetch('/recommendations/api/library-row', {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' }
    })
      .then(r => r.json())
      .then(payload => {
        const data = (payload && payload.data) || [];
        if (!data.length) { section.remove(); return; }
        renderInto(row, data, hideReason);
        section.hidden = false;
      })
      .catch(err => {
        console.warn('Library recommendations failed:', err);
        section.remove();
      });
  }
  loadInto('library-recs-section', 'library-recs-row', /*hideReason*/ true);
  loadInto('library-empty-recs-section', 'library-empty-recs-row', /*hideReason*/ false);
})();
</script>
```

- [ ] **Step 5: Manual smoke test**

```bash
docker cp app/templates/library.html mybibliotheca-bibliotheca-1:/app/app/templates/library.html
docker compose -f docker-compose.dev.yml restart bibliotheca
```

Open http://localhost:5054/library:
- With finished books → "Recommended for you" row appears above the filter bar.
- Apply a filter or search → row hides.
- (Optional) clear all books to verify empty-state seeding shows the popular section.

- [ ] **Step 6: Commit**

```bash
git add app/templates/library.html
git commit -m "feat(recs): library top row + empty-state seeding"
```

---

## Task 14: "Discover" nav link

**Files:**
- Modify: `app/templates/base.html`

- [ ] **Step 1: Add the nav link**

Find the line `<a class="nav-link nav-pill" href="{{ url_for('main.stats') }}">Stats</a>` (around line 1676 in current state) and insert immediately before it:

```jinja
              <a class="nav-link nav-pill" href="{{ url_for('recommendations.page') }}">Discover</a>
```

- [ ] **Step 2: Verify the URL builds**

```bash
docker cp app/templates/base.html mybibliotheca-bibliotheca-1:/app/app/templates/base.html
docker compose -f docker-compose.dev.yml restart bibliotheca
```

Hit http://localhost:5054/library → confirm "Discover" appears in the nav and clicking it lands on `/recommendations/`.

- [ ] **Step 3: Commit**

```bash
git add app/templates/base.html
git commit -m "feat(recs): Discover nav link"
```

---

## Task 15: End-to-end manual smoke test

**No files.** Verify the whole feature in the running dev container.

- [ ] **Step 1: Run the full test suite**

```bash
pytest tests/test_recommendation_scorer.py tests/test_recommendation_service.py tests/test_recommendation_routes.py -v
```

Expected: all tests pass.

- [ ] **Step 2: Walk through the surfaces in the browser**

Log in, then check:

- [ ] `/recommendations` page renders with three sections (or the cold-start banner + popular if you're a new user).
- [ ] Open any book detail page → "More like this" section appears with cards (or is hidden if fewer than 4 results).
- [ ] Library page top row shows "Recommended for you".
- [ ] Apply a filter → top row hides.
- [ ] Clear all books → empty state shows the popular seed (skip if you don't want to wipe data).
- [ ] Click any recommendation → lands on the book's detail page.

- [ ] **Step 3: Commit anything you adjusted during smoke test**

If you tweaked CSS or copy during the walk-through, stage and commit:

```bash
git add -u
git commit -m "polish(recs): smoke-test adjustments"
```

If nothing changed, no commit needed.

---

## Done.

The feature is shippable when:
1. All three test files pass (`pytest tests/test_recommendation_*.py -v`).
2. Manual walk-through above is green.
3. No new lint errors (`python3 -m py_compile` on every changed file is clean).

If anything fails or feels off (slow first-request, weird recs for the seed
graph, layout glitches), file follow-ups — don't paper over them in this
implementation.
