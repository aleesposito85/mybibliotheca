#!/usr/bin/env python3
"""
Database migration script to add security and privacy fields to User model
Adds: failed_login_attempts, locked_until, last_login, share_current_reading, 
      share_reading_activity, share_library
"""

import os
import sys
import sqlite3
from datetime import datetime, timezone

def migrate_database(db_path='data/books.db'):
    """Add new fields to User table"""
    
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        return False

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        print("Adding security and privacy fields to User table...")
        
        # Check if columns already exist
        cursor.execute("PRAGMA table_info(user)")
        existing_columns = [column[1] for column in cursor.fetchall()]
        
        # Add security fields
        if 'failed_login_attempts' not in existing_columns:
            cursor.execute('ALTER TABLE user ADD COLUMN failed_login_attempts INTEGER DEFAULT 0')
            print("✓ Added failed_login_attempts column")
        
        if 'locked_until' not in existing_columns:
            cursor.execute('ALTER TABLE user ADD COLUMN locked_until DATETIME')
            print("✓ Added locked_until column")
        
        if 'last_login' not in existing_columns:
            cursor.execute('ALTER TABLE user ADD COLUMN last_login DATETIME')
            print("✓ Added last_login column")
        
        # Add privacy fields
        if 'share_current_reading' not in existing_columns:
            cursor.execute('ALTER TABLE user ADD COLUMN share_current_reading BOOLEAN DEFAULT 1')
            print("✓ Added share_current_reading column")
        
        if 'share_reading_activity' not in existing_columns:
            cursor.execute('ALTER TABLE user ADD COLUMN share_reading_activity BOOLEAN DEFAULT 1')
            print("✓ Added share_reading_activity column")
        
        if 'share_library' not in existing_columns:
            cursor.execute('ALTER TABLE user ADD COLUMN share_library BOOLEAN DEFAULT 1')
            print("✓ Added share_library column")
        
        conn.commit()
        print("✅ Database migration completed successfully!")
        
        # Verify the changes
        cursor.execute("PRAGMA table_info(user)")
        columns = cursor.fetchall()
        print(f"\nUpdated User table now has {len(columns)} columns:")
        for col in columns:
            print(f"  - {col[1]} ({col[2]})")
        
        return True
        
    except sqlite3.Error as e:
        print(f"❌ Database error: {e}")
        return False
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return False
    finally:
        if conn:
            conn.close()

def main():
    """Main migration function"""
    print("MyBibliotheca - User Security & Privacy Migration")
    print("=" * 50)
    
    # Check if running from correct directory
    if not os.path.exists('app'):
        print("❌ Please run this script from the project root directory")
        sys.exit(1)
    
    # Backup database first
    db_path = 'data/books.db'
    if os.path.exists(db_path):
        backup_path = f'data/books_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db'
        try:
            import shutil
            shutil.copy2(db_path, backup_path)
            print(f"✓ Database backed up to {backup_path}")
        except Exception as e:
            print(f"⚠️  Could not create backup: {e}")
            response = input("Continue without backup? (y/N): ")
            if response.lower() != 'y':
                sys.exit(1)
    
    # Run migration
    success = migrate_database(db_path)
    
    if success:
        print("\n🎉 Migration completed successfully!")
        print("\nNew features available:")
        print("  • Account lockout after 5 failed login attempts")
        print("  • Admin password reset capabilities")
        print("  • User privacy settings for sharing preferences")
        print("  • Enhanced user activity tracking")
    else:
        print("\n❌ Migration failed. Please check the error messages above.")
        sys.exit(1)

if __name__ == '__main__':
    main()
