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
    # series_next_volume is always more specific than a plain series match;
    # if it has any contribution at all, prefer it over series for the reason
    # even if series has a higher raw weight.
    snv_value = contributions.get("series_next_volume", 0.0)
    if snv_value > 0:
        volume = sig.get("next_volume_number")
        series_name = sig.get("series_name") or "this series"
        if volume is not None:
            return f"Volume {volume} of {series_name}"
        return f"Next in {series_name}"

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
    """For each candidate, count distinct categories shared with any anchor.

    Groups by candidate book only so ``count`` reflects the true number of
    overlapping categories. The collected names list also gives us a
    representative label for the reason string without a second query.

    KuzuDB gotcha: ``RETURN c.id, cat.name, count(DISTINCT cat)`` groups by
    *all* non-aggregate columns, yielding count=1 per row. We must group by
    ``c.id`` alone and use ``collect(DISTINCT cat.name)`` instead.
    """
    if not anchors:
        return {}
    try:
        result = safe_execute_kuzu_query(
            "MATCH (a:Book) WHERE a.id IN $anchors "
            "MATCH (a)-[:CATEGORIZED_AS]->(cat:Category)<-[:CATEGORIZED_AS]-(c:Book) "
            "WHERE c.id <> a.id "
            "RETURN c.id AS book_id, collect(DISTINCT cat.name) AS cats",
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
        cats = row.get("cats") or []
        out[bid] = {
            "count": len(cats),
            "top_category_name": cats[0] if cats else None,
        }
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


# ----- Service skeleton ------------------------------------------------

class KuzuRecommendationService:
    """See module docstring. Public methods: get_more_like_this_sync,
    get_library_row_sync, get_top_picks_sync, get_continue_series_sync,
    get_popular_sync, get_recommendations_page_sync.
    """

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
