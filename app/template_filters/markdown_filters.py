"""
Template filters for markdown rendering.
"""
import logging
import re
import mistune
from markupsafe import Markup, escape

logger = logging.getLogger(__name__)

# Create a markdown instance with safe defaults
# escape=True ensures user-provided HTML is escaped for security
markdown = mistune.create_markdown(
    escape=True,  # Escape HTML to prevent XSS
    plugins=['strikethrough', 'table', 'url']
)


# Match the href= attribute on any <a> tag the markdown engine produced.
# We post-process to strip dangerous schemes (javascript:, data:, vbscript:) —
# the mistune `url` autolink plugin does not block them by default.
_HREF_RE = re.compile(r'(<a\b[^>]*?\shref=)(["\'])([^"\']*)\2', re.IGNORECASE)
_SAFE_HREF_RE = re.compile(r'^(https?:|mailto:|/|#)', re.IGNORECASE)


def _sanitize_hrefs(html: str) -> str:
    def _scrub(m):
        prefix, quote, url = m.group(1), m.group(2), m.group(3).strip()
        if _SAFE_HREF_RE.match(url):
            return m.group(0)
        # Drop the unsafe href (keeping the rest of the tag intact).
        return f'{prefix}{quote}#{quote}'
    return _HREF_RE.sub(_scrub, html)


def render_markdown(text):
    """
    Convert markdown text to HTML.

    Args:
        text: Markdown text to convert (should be a string)

    Returns:
        Safe HTML markup
    """
    if not text:
        return ''

    # Ensure text is a string
    if not isinstance(text, str):
        text = str(text)

    try:
        # Convert markdown to HTML (with HTML escaping enabled for security)
        html = markdown(text)
        # Strip javascript:/data:/vbscript: URLs that the autolink plugin lets
        # through; book descriptions imported from external feeds end up here.
        html = _sanitize_hrefs(html)
        return Markup(html)
    except Exception as e:
        logger.warning(f"Markdown rendering failed: {type(e).__name__}: {e}")
        return Markup(f'<p>{escape(str(text))}</p>')
