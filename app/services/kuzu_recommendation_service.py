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
