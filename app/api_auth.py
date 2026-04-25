"""
API Authentication Module

Provides secure authentication for API endpoints using API tokens
while maintaining CSRF protection for web interface.
"""

import secrets
import hashlib
from datetime import datetime, timedelta
from functools import wraps
from flask import request, jsonify, current_app
from flask_login import current_user


class APIToken:
    """API Token management for secure API access."""
    
    @staticmethod
    def generate_token(user_id: str, name: str = "API Token") -> tuple[str, str]:
        """
        Generate a new API token for a user.
        Returns (token, hashed_token) tuple.
        """
        # Generate a secure random token
        token = secrets.token_urlsafe(32)
        
        # Hash the token for storage (never store plain tokens)
        hashed_token = hashlib.sha256(token.encode()).hexdigest()
        
        return token, hashed_token
    
    @staticmethod
    def verify_token(token: str, hashed_token: str) -> bool:
        """Verify if a token matches its hash."""
        if not token or not hashed_token:
            return False
        
        computed_hash = hashlib.sha256(token.encode()).hexdigest()
        return secrets.compare_digest(computed_hash, hashed_token)


def _bind_token_user():
    """If API_TOKEN_USER_ID is configured, log that user in for the request.

    This binds the validated token to a real user so that downstream code
    referencing ``current_user.id`` doesn't land on the AnonymousUser proxy.
    Returns True on successful bind, False otherwise.
    """
    import logging
    logger = logging.getLogger(__name__)
    user_id = current_app.config.get('API_TOKEN_USER_ID')
    if not user_id:
        logger.warning("Bearer token accepted but API_TOKEN_USER_ID not configured — refusing.")
        return False
    try:
        from .services import user_service
        from flask_login import login_user
        user = user_service.get_user_by_id_sync(str(user_id))
        if not user:
            logger.warning(f"API_TOKEN_USER_ID={user_id} does not resolve to a user — refusing.")
            return False
        login_user(user, remember=False)
        return True
    except Exception as e:
        logger.error(f"Failed to bind API token to user {user_id}: {e}")
        return False


def api_token_required(f):
    """
    Decorator for API endpoints that require token authentication.
    This bypasses CSRF protection for API calls while maintaining security.
    Validates token directly without relying on Flask-Login sessions.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        import logging
        logger = logging.getLogger(__name__)

        # Check if this is an API request
        if request.path.startswith('/api/'):
            logger.info(f"API request to {request.path}")
            # First check for token authentication
            auth_header = request.headers.get('Authorization')

            if auth_header and auth_header.startswith('Bearer '):
                token = auth_header.split(' ', 1)[1]
                if validate_api_token(token):
                    # The token is shared; without a configured user binding we
                    # cannot answer "whose data is this?" — refuse rather than
                    # silently fall into an AnonymousUser proxy.
                    if not _bind_token_user():
                        return jsonify({'error': 'API token user not configured'}), 500
                    logger.info("Token validation successful, allowing access")
                    return f(*args, **kwargs)
                else:
                    logger.info("Token validation failed")
                    return jsonify({'error': 'Invalid API token'}), 401

            # No token provided, check if there's an active session
            from flask_login import current_user
            try:
                if hasattr(current_user, 'is_authenticated') and current_user.is_authenticated:
                    return f(*args, **kwargs)
            except Exception as e:
                logger.info(f"Session check failed: {e}")

            return jsonify({
                'error': 'Authentication required',
                'message': 'Provide API token via Authorization header or login via web interface',
                'authentication_methods': [
                    'Bearer token in Authorization header',
                    'Session-based login via web interface'
                ]
            }), 401

        # For non-API requests, fall back to regular authentication check
        from flask_login import current_user
        if not current_user.is_authenticated:
            return jsonify({'error': 'Authentication required'}), 401

        return f(*args, **kwargs)

    return decorated_function


def validate_api_token(token: str) -> bool:
    """Validate an API token against the configured value.

    Fails closed: if no API_TEST_TOKEN is configured the API rejects every
    bearer token. This removes the previous hardcoded ``dev-token-12345``
    fallback that would have granted full access if any deployment ever set
    the env var to that literal value.
    """
    import logging
    logger = logging.getLogger(__name__)

    if not token:
        return False

    expected = current_app.config.get('API_TEST_TOKEN')
    if not expected:
        logger.warning("API_TEST_TOKEN not configured — bearer-token auth disabled.")
        return False
    return secrets.compare_digest(token, expected)


def api_auth_optional(f):
    """
    Decorator for API endpoints where authentication is optional.
    Still bypasses CSRF for API calls but doesn't require authentication.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check for API token
        auth_header = request.headers.get('Authorization')

        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ', 1)[1]
            if not validate_api_token(token):
                return jsonify({'error': 'Invalid API token'}), 401
            _bind_token_user()  # best-effort; optional auth tolerates anon

        # Proceed regardless of authentication status
        return f(*args, **kwargs)

    return decorated_function
