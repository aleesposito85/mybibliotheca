"""
Reading Log API Endpoints

Provides RESTful CRUD operations for reading logs using the dual-write service layer.
Uses secure API token authentication to bypass CSRF for legitimate API calls.
"""

from flask import Blueprint, request, jsonify, current_app
from flask_login import current_user
from datetime import datetime, date
import traceback

from ..api_auth import api_token_required, api_auth_optional
from ..services import reading_log_service
from ..domain.models import ReadingLog as DomainReadingLog
from ..utils.user_settings import get_effective_reading_defaults

# Create API blueprint
reading_logs_api = Blueprint('reading_logs_api', __name__, url_prefix='/api/v1/reading-logs')


def serialize_reading_log(log):
    """Convert a reading log (domain object or dict) to API response format."""
    def _iso(v):
        try:
            return v.isoformat()
        except Exception:
            return v

    if isinstance(log, dict):
        return {
            'id': log.get('id'),
            'book_id': log.get('book_id'),
            'user_id': log.get('user_id'),
            'date': _iso(log.get('date')) if log.get('date') else None,
            'pages_read': log.get('pages_read'),
            'minutes_read': log.get('minutes_read'),
            'notes': log.get('notes'),
            'created_at': _iso(log.get('created_at')) if log.get('created_at') else None,
            'updated_at': _iso(log.get('updated_at')) if log.get('updated_at') else None,
        }
    else:
        # Assume DomainReadingLog dataclass or similar
        return {
            'id': getattr(log, 'id', None),
            'book_id': getattr(log, 'book_id', None),
            'user_id': getattr(log, 'user_id', None),
            'date': _iso(getattr(log, 'date', None)) if getattr(log, 'date', None) else None,
            'pages_read': getattr(log, 'pages_read', None),
            'minutes_read': getattr(log, 'minutes_read', None),
            'notes': getattr(log, 'notes', None),
            'created_at': _iso(getattr(log, 'created_at', None)) if getattr(log, 'created_at', None) else None,
            'updated_at': _iso(getattr(log, 'updated_at', None)) if getattr(log, 'updated_at', None) else None,
        }


@reading_logs_api.route('', methods=['GET'])
@api_token_required
def get_reading_logs():
    """Get reading logs for the current user."""
    try:
        # Get query parameters
        book_id = request.args.get('book_id')
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        
        # Parse dates if provided
        parsed_start_date = None
        parsed_end_date = None
        
        if start_date:
            try:
                parsed_start_date = datetime.fromisoformat(start_date).date()
            except ValueError:
                return jsonify({
                    'status': 'error',
                    'message': 'Invalid start_date format. Use YYYY-MM-DD'
                }), 400
        
        if end_date:
            try:
                parsed_end_date = datetime.fromisoformat(end_date).date()
            except ValueError:
                return jsonify({
                    'status': 'error',
                    'message': 'Invalid end_date format. Use YYYY-MM-DD'
                }), 400
        
        # Pull a generous window from the service then apply explicit filters here.
        # service.get_user_reading_logs_sync uses days_back; if a start_date is
        # given, derive the matching window; otherwise fetch the default window.
        if parsed_start_date:
            days_back = max(1, (date.today() - parsed_start_date).days + 1)
        else:
            days_back = 365
        raw_logs = reading_log_service.get_user_reading_logs_sync(
            user_id=str(current_user.id),
            days_back=days_back,
            limit=None,
        )

        def _matches(log):
            log_date = log.get('date')
            if hasattr(log_date, 'isoformat'):
                pass  # already a date
            elif isinstance(log_date, str):
                try:
                    log_date = datetime.fromisoformat(log_date).date()
                except ValueError:
                    return False
            if parsed_start_date and log_date and log_date < parsed_start_date:
                return False
            if parsed_end_date and log_date and log_date > parsed_end_date:
                return False
            if book_id and str(log.get('book_id') or (log.get('book') or {}).get('id') or '') != str(book_id):
                return False
            return True

        reading_logs = [serialize_reading_log(l) for l in raw_logs if _matches(l)]

        return jsonify({
            'status': 'success',
            'data': reading_logs,
            'count': len(reading_logs),
            'filters': {
                'book_id': book_id,
                'start_date': start_date,
                'end_date': end_date
            }
        }), 200
        
    except Exception as e:
        current_app.logger.error(f"Error getting reading logs: {e}")
        current_app.logger.error(traceback.format_exc())
        return jsonify({
            'status': 'error',
            'message': 'Failed to retrieve reading logs',
            'error': str(e)
        }), 500


@reading_logs_api.route('', methods=['POST'])
@api_token_required
def create_reading_log():
    """Create a new reading log entry. Applies per-user or admin defaults if both pages and minutes are missing."""
    try:
        if not request.json:
            return jsonify({
                'status': 'error',
                'message': 'JSON data required'
            }), 400
        
        data = request.json
        
        # Validate required fields
        if not data.get('book_id'):
            return jsonify({
                'status': 'error',
                'message': 'book_id is required'
            }), 400
        
        # Parse date
        log_date = date.today()  # Default to today
        if 'date' in data and data['date']:
            try:
                log_date = datetime.fromisoformat(data['date']).date()
            except ValueError:
                return jsonify({
                    'status': 'error',
                    'message': 'Invalid date format. Use YYYY-MM-DD'
                }), 400

        # Parse metrics
        try:
            pages_read = int(data.get('pages_read', 0) or 0)
            minutes_read = int(data.get('minutes_read', 0) or 0)
        except (TypeError, ValueError):
            return jsonify({
                'status': 'error',
                'message': 'pages_read and minutes_read must be integers'
            }), 400

        # Apply defaults if both are zero/missing
        if pages_read <= 0 and minutes_read <= 0:
            dp, dm = get_effective_reading_defaults(getattr(current_user, 'id', None))
            if (dp or 0) > 0:
                pages_read = int(dp)  # type: ignore[arg-type]
            if (dm or 0) > 0:
                minutes_read = int(dm)  # type: ignore[arg-type]
        # Validate that at least one metric is provided
        if pages_read <= 0 and minutes_read <= 0:
            return jsonify({
                'status': 'error',
                'message': 'Provide either pages_read or minutes_read'
            }), 400

        # Optional notes
        notes = (data.get('notes') or '').strip() or None

        # Create reading log using service layer
        rl = DomainReadingLog(
            user_id=str(current_user.id),
            book_id=str(data['book_id']) if data.get('book_id') else None,
            date=log_date,
            pages_read=pages_read,
            minutes_read=minutes_read,
            notes=notes
        )
        domain_log = reading_log_service.create_reading_log_sync(rl)

        return jsonify({
            'status': 'success',
            'message': 'Reading log created successfully',
            'data': serialize_reading_log(domain_log)
        }), 201
        
    except Exception as e:
        current_app.logger.error(f"Error creating reading log: {e}")
        current_app.logger.error(traceback.format_exc())
        return jsonify({
            'status': 'error',
            'message': 'Failed to create reading log',
            'error': str(e)
        }), 500


@reading_logs_api.route('/check', methods=['POST'])
@api_token_required
def check_existing_log():
    """Check if a reading log already exists for a specific date."""
    try:
        if not request.json:
            return jsonify({
                'status': 'error',
                'message': 'JSON data required'
            }), 400
        
        data = request.json
        
        # Validate required fields
        if not data.get('book_id'):
            return jsonify({
                'status': 'error',
                'message': 'book_id is required'
            }), 400
        
        # Parse date
        log_date = date.today()  # Default to today
        if 'date' in data and data['date']:
            try:
                log_date = datetime.fromisoformat(data['date']).date()
            except ValueError:
                return jsonify({
                    'status': 'error',
                    'message': 'Invalid date format. Use YYYY-MM-DD'
                }), 400
        
        # Check for existing log
        existing_log = reading_log_service.get_existing_log_sync(
            book_id=str(data['book_id']),
            user_id=current_user.id,
            log_date=log_date
        )
        
        return jsonify({
            'status': 'success',
            'exists': existing_log is not None,
            'data': serialize_reading_log(existing_log) if existing_log else None
        }), 200
        
    except Exception as e:
        current_app.logger.error(f"Error checking reading log: {e}")
        current_app.logger.error(traceback.format_exc())
        return jsonify({
            'status': 'error',
            'message': 'Failed to check reading log',
            'error': str(e)
        }), 500


@reading_logs_api.route('/<log_id>', methods=['DELETE'])
@api_token_required
def delete_reading_log(log_id):
    """Delete a reading log entry."""
    try:
        deleted = reading_log_service.delete_reading_log_sync(
            log_id=str(log_id),
            user_id=str(current_user.id),
        )
        if not deleted:
            return jsonify({
                'status': 'error',
                'message': 'Reading log not found'
            }), 404

        return jsonify({
            'status': 'success',
            'message': 'Reading log deleted successfully'
        }), 200
        
    except Exception as e:
        current_app.logger.error(f"Error deleting reading log {log_id}: {e}")
        current_app.logger.error(traceback.format_exc())
        return jsonify({
            'status': 'error',
            'message': 'Failed to delete reading log',
            'error': str(e)
        }), 500
