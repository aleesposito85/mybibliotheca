"""
Web-based migration interface for SQLite to Redis migration.
Provides a user-friendly wizard interface for migrating data.
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app
from flask_login import login_required, current_user
import asyncio
import json
import os
import tempfile
import logging
from datetime import datetime

from .migration_detector import MigrationDetector
from .services import user_service

logger = logging.getLogger(__name__)

# Create the migration blueprint
migration_bp = Blueprint('migration', __name__, url_prefix='/migration')

@migration_bp.route('/check')
def check_migration_status():
    """API endpoint to check if migration is needed."""
    try:
        detector = MigrationDetector()
        databases = detector.find_sqlite_databases()
        
        if not databases:
            return jsonify({
                'migration_needed': False,
                'message': 'No SQLite databases found'
            })
        
        database_info = []
        total_books = 0
        total_users = 0
        
        for db_path in databases:
            info = detector.analyze_database(db_path)
            database_info.append({
                'path': db_path,
                'filename': os.path.basename(db_path),
                'version': info.get('version', 'unknown'),
                'books': info.get('total_books', 0),
                'users': info.get('total_users', 0),
                'reading_logs': info.get('total_reading_logs', 0)
            })
            total_books += info.get('total_books', 0)
            total_users += info.get('total_users', 0)
        
        return jsonify({
            'migration_needed': True,
            'databases': database_info,
            'summary': {
                'total_databases': len(databases),
                'total_books': total_books,
                'total_users': total_users
            }
        })
    
    except Exception as e:
        return jsonify({
            'migration_needed': False,
            'error': str(e)
        }), 500

@migration_bp.route('/wizard')
@login_required
def migration_wizard():
    """Main migration wizard page."""
    try:
        detector = MigrationDetector()
        databases = detector.find_sqlite_databases()
        
        if not databases:
            flash('No SQLite databases found that need migration.', 'info')
            return redirect(url_for('main.index'))
        
        database_info = []
        for db_path in databases:
            info = detector.analyze_database(db_path)
            database_info.append({
                'path': db_path,
                'filename': os.path.basename(db_path),
                'version': info.get('version', 'unknown'),
                'books': info.get('total_books', 0),
                'users': info.get('total_users', 0),
                'reading_logs': info.get('total_reading_logs', 0),
                'size_mb': round(os.path.getsize(db_path) / (1024 * 1024), 2)
            })
        
        # For migration, we always use the current logged-in user
        # No need to show complex user selection - much simpler!
        
        return render_template('migration/wizard.html', 
                             databases=database_info,
                             current_user_name=getattr(current_user, 'username', 'Admin'))
    
    except Exception as e:
        flash(f'Error detecting databases: {str(e)}', 'danger')
        return redirect(url_for('main.index'))

@migration_bp.route('/configure', methods=['POST'])
@login_required
def configure_migration():
    """Configure migration settings."""
    try:
        selected_databases = request.form.getlist('selected_databases')
        create_backup = request.form.get('create_backup') == 'on'
        delete_after_migration = request.form.get('delete_after_migration') == 'on'
        
        if not selected_databases:
            flash('Please select at least one database to migrate.', 'warning')
            return redirect(url_for('migration.migration_wizard'))
        
        # Validate databases exist
        detector = MigrationDetector()
        all_databases = detector.find_sqlite_databases()
        all_database_paths = [str(db) for db in all_databases]
        valid_databases = [db for db in selected_databases if db in all_database_paths]
        
        if not valid_databases:
            flash('Selected databases are no longer available.', 'danger')
            return redirect(url_for('migration.migration_wizard'))
        
        # Get user-friendly display name
        display_user_name = getattr(current_user, 'username', None)
        if not display_user_name:
            display_user_name = "Current Admin User"
        
        logger.info(f"🚀 Migration configured for user: {display_user_name} (ID: {current_user.id})")
        
        # Store configuration in session for the migration process
        migration_config = {
            'databases': valid_databases,
            'user_display_name': display_user_name,
            'create_backup': create_backup,
            'delete_after_migration': delete_after_migration,
            'timestamp': datetime.now().isoformat()
        }
        
        # Store in a temporary file for the migration process
        config_file = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(migration_config, config_file, indent=2)
        config_file.close()
        
        return render_template('migration/confirm.html',
                             config=migration_config,
                             config_file=config_file.name)
    
    except Exception as e:
        flash(f'Error configuring migration: {str(e)}', 'danger')
        return redirect(url_for('migration.migration_wizard'))

@migration_bp.route('/execute', methods=['POST'])
@login_required
def execute_migration():
    """Execute the migration process."""
    config_file = request.form.get('config_file')
    
    if not config_file or not os.path.exists(config_file):
        flash('Migration configuration not found.', 'danger')
        return redirect(url_for('migration.migration_wizard'))
    
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)

        # Create a unique migration ID for tracking
        migration_id = f"migration_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        return render_template('migration/progress.html',
                             migration_id=migration_id,
                             config=config,
                             config_file=config_file)
    
    except Exception as e:
        flash(f'Error starting migration: {str(e)}', 'danger')
        return redirect(url_for('migration.migration_wizard'))

@migration_bp.route('/run/<migration_id>', methods=['POST'])
@login_required
def run_migration(migration_id):
    """API endpoint to actually run the migration."""
    try:
        if not request.json:
            return jsonify({'error': 'No JSON data provided'}), 400
            
        config_file = request.json.get('config_file')

        if not config_file or not os.path.exists(config_file):
            return jsonify({'error': 'Configuration file not found'}), 400

        try:
            # Read & validate the config (we still want a clean error if it's malformed),
            # but the legacy SQLite→Redis migration is no longer supported.
            with open(config_file, 'r', encoding='utf-8') as f:
                json.load(f)

            return jsonify({
                'success': False,
                'error': 'Redis migration is no longer supported. Users start fresh with Kuzu database.',
                'results': []
            })
        finally:
            # Always remove the temp config file so it doesn't leak in /tmp.
            try:
                os.unlink(config_file)
            except OSError:
                pass

    except Exception as e:
        logger.exception("Error in run_migration")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@migration_bp.route('/success')
@login_required
def migration_success():
    """Migration completion success page."""
    return render_template('migration/success.html')

@migration_bp.route('/dismiss', methods=['POST'])
@login_required
def dismiss_migration():
    """Allow users to dismiss the migration reminder."""
    from flask import session
    session['migration_dismissed'] = True
    flash('Migration reminder dismissed. You can always access migration from the admin panel.', 'info')
    return redirect(url_for('main.index'))
