"""Bookshelf scanner blueprint.

All endpoints require login. The upload route is synchronous (~30-80s for
the LLM call); the confirm route kicks off a background bulk-add via the
existing safe_import_manager and returns a task_id. The progress page is
the existing /import/progress/<task_id> page (we just feed the same job
shape into it).
"""
from __future__ import annotations

import logging

from flask import (
    Blueprint, current_app, flash, jsonify, redirect, render_template, request,
    url_for,
)
from flask_login import current_user, login_required

from app.services import shelf_scan_service
from app.services.shelf_scan_service import (
    ShelfScanLLMUnavailable, ShelfScanEmptyResult, ShelfScanRateLimited,
    ShelfScanInProgress, ShelfScanError,
)

logger = logging.getLogger(__name__)

shelf_scan_bp = Blueprint("shelf_scan", __name__, url_prefix="/books/scan")


def _ai_provider_label() -> dict:
    """Build the user-facing AI-provider notice for the upload page."""
    import os
    provider = os.environ.get("AI_PROVIDER", "ollama").lower()
    ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    ollama_model = os.environ.get("OLLAMA_MODEL", "llama3.2-vision")
    openai_model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_ollama = bool(ollama_url)
    if provider == "openai" and has_openai:
        return {"provider": "openai", "model": openai_model, "configured": True,
                "label": f"Using OpenAI Vision ({openai_model}) — ~$0.02 per scan."}
    if provider == "ollama" and has_ollama:
        return {"provider": "ollama", "model": ollama_model, "configured": True,
                "label": f"Using local Ollama at {ollama_url} (model {ollama_model})."}
    if has_openai:
        return {"provider": "openai", "model": openai_model, "configured": True,
                "label": f"Using OpenAI Vision ({openai_model}) — ~$0.02 per scan."}
    if has_ollama:
        return {"provider": "ollama", "model": ollama_model, "configured": True,
                "label": f"Using local Ollama at {ollama_url} (model {ollama_model})."}
    return {"provider": None, "model": None, "configured": False,
            "label": "No AI provider configured. Set AI_PROVIDER, OLLAMA_BASE_URL, or OPENAI_API_KEY."}


@shelf_scan_bp.route("/", methods=["GET"])
@login_required
def upload_page():
    return render_template("shelf_scan_upload.html",
                           ai=_ai_provider_label(),
                           error=None,
                           preview_url=None)


@shelf_scan_bp.route("/upload", methods=["POST"])
@login_required
def upload():
    file = request.files.get("shelf_image")
    if not file or file.filename == "":
        flash("Please choose an image to upload.", "warning")
        return redirect(url_for("shelf_scan.upload_page"))
    image_bytes = file.read()
    user_id = str(current_user.id)
    try:
        result = shelf_scan_service.scan_image_and_enrich_sync(
            image_bytes=image_bytes,
            user_id=user_id,
            original_filename=file.filename,
        )
    except ShelfScanLLMUnavailable:
        return render_template("shelf_scan_upload.html",
                               ai=_ai_provider_label(),
                               error="Vision model is unavailable. Check that Ollama is running, "
                                     "or set AI_PROVIDER=openai with a key.",
                               preview_url=None)
    except ShelfScanEmptyResult as e:
        return render_template("shelf_scan_upload.html",
                               ai=_ai_provider_label(),
                               error="We couldn't read any spines in that photo. "
                                     "Try a clearer, well-lit photo.",
                               preview_url=e.preview_url)
    except ShelfScanRateLimited as e:
        return render_template("shelf_scan_upload.html",
                               ai=_ai_provider_label(),
                               error=str(e),
                               preview_url=None), 429
    except ShelfScanInProgress as e:
        return render_template("shelf_scan_upload.html",
                               ai=_ai_provider_label(),
                               error=str(e),
                               preview_url=None), 409
    except ValueError as e:
        return render_template("shelf_scan_upload.html",
                               ai=_ai_provider_label(),
                               error=f"Image validation failed: {e}",
                               preview_url=None), 400
    except Exception:
        logger.exception("shelf_scan upload failed")
        return render_template("shelf_scan_upload.html",
                               ai=_ai_provider_label(),
                               error="An unexpected error occurred. Please try again.",
                               preview_url=None), 500

    return render_template("shelf_scan_confirm.html", **result)


@shelf_scan_bp.route("/confirm", methods=["POST"])
@login_required
def confirm():
    import json as _json
    scan_id = request.form.get("scan_id", "").strip()
    picked = request.form.getlist("detection_id")
    try:
        overrides = _json.loads(request.form.get("overrides") or "{}")
    except _json.JSONDecodeError:
        overrides = {}
    user_id = str(current_user.id)
    if not scan_id:
        return jsonify({"status": "error", "message": "scan_id required"}), 400
    if not picked:
        return jsonify({"status": "error", "message": "no books selected"}), 400
    if shelf_scan_service.get_scan(scan_id, user_id) is None:
        return jsonify({"status": "error", "message": "Scan expired or not found"}), 410
    try:
        task_id = shelf_scan_service.start_bulk_add_async(
            user_id=user_id,
            scan_id=scan_id,
            picked=picked,
            overrides=overrides,
        )
    except ShelfScanError as e:
        return jsonify({"status": "error", "message": str(e)}), 410
    return jsonify({"status": "success", "task_id": task_id})


@shelf_scan_bp.route("/progress/<task_id>", methods=["GET"])
@login_required
def progress(task_id: str):
    from app.utils.safe_import_manager import safe_get_import_job
    user_id = str(current_user.id)
    job = safe_get_import_job(user_id, task_id)
    if not job:
        return jsonify({"status": "error", "message": "task not found"}), 404
    return jsonify({"status": "success", "job": job})


@shelf_scan_bp.route("/<scan_id>/discard", methods=["POST"])
@login_required
def discard(scan_id: str):
    user_id = str(current_user.id)
    ok = shelf_scan_service.discard_scan(scan_id, user_id)
    if not ok:
        return jsonify({"status": "error", "message": "scan not found"}), 404
    return jsonify({"status": "success"})


# ---- Admin health check ------------------------------------------------

shelf_scan_admin_bp = Blueprint("shelf_scan_admin", __name__, url_prefix="/admin/scan")


@shelf_scan_admin_bp.route("/health", methods=["GET"])
@login_required
def health():
    """Probe the configured AI provider with a 1x1 dummy image. Cached 5 min."""
    if not getattr(current_user, "is_admin", False):
        return jsonify({"status": "error", "message": "admin required"}), 403
    import io as _io, base64 as _b64, time as _time
    from PIL import Image as _Image
    from app.services.ai_service import AIService
    from app.services.shelf_scan_service import _load_ai_config

    # 5-min in-memory cache, keyed by provider.
    cache = getattr(health, "_cache", {})
    health._cache = cache  # type: ignore[attr-defined]
    cfg = _load_ai_config()
    provider = cfg.get("AI_PROVIDER", "ollama")
    cached = cache.get(provider)
    if cached and (_time.time() - cached["ts"] < 300):
        return jsonify(cached["payload"])

    # Build a tiny PNG (1x1) and ask the model to extract — we don't care
    # about the result, only that the call returns 200.
    img = _Image.new("RGB", (1, 1), color=(255, 255, 255))
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    image_bytes = buf.getvalue()

    ai = AIService(cfg)
    t0 = _time.time()
    try:
        ai.extract_books_from_shelf_image(image_bytes)
        ok = True
        err = None
    except Exception as e:
        ok = False
        err = str(e)
    latency_ms = int((_time.time() - t0) * 1000)
    payload = {
        "provider": provider,
        "model": cfg.get("OLLAMA_MODEL") if provider == "ollama" else cfg.get("OPENAI_MODEL"),
        "ok": ok,
        "latency_ms": latency_ms,
        "error": err,
    }
    cache[provider] = {"ts": _time.time(), "payload": payload}
    return jsonify(payload)
