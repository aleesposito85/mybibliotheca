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
