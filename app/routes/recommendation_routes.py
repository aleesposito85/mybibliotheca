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
