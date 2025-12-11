import sqlite3
import os
from app import app, db

def migrate():
    with app.app_context():
        # SQLite doesn't support "IF NOT EXISTS" for ADD COLUMN in older versions, 
        # but we can check PRAGMA table_info or just try/except.
        # Since we might be on Postgres in production (Render), we should use SQLAlchemy engine or raw SQL compatible with both if possible, 
        # but for this app structure we often use raw SQL for quick migrations.
        # Let's use the engine to be safe across DB types if configured, but here we likely use the default DB config.
        
        print("Running migration: Add description to activity table...")
        
        try:
            with db.engine.connect() as conn:
                trans = conn.begin()
                try:
                    # Check if column exists is harder in generic SQL.
                    # Simplest way: Try to select it, if fails, add it.
                    try:
                        conn.execute(db.text("SELECT description FROM activity LIMIT 1"))
                        print("Column 'description' already exists. Skipping.")
                    except Exception:
                        print("Column 'description' not found. Adding it...")
                        # Add column
                        # Note: TEXT is good for description. Nullable=True.
                        conn.execute(db.text("ALTER TABLE activity ADD COLUMN description TEXT"))
                        print("Column added successfully.")
                        
                    trans.commit()
                    print("Migration completed.")
                except Exception as e:
                    trans.rollback()
                    print(f"Migration failed during execution: {e}")
                    raise e
        except Exception as e:
             print(f"Connection/Migration Error: {e}")

if __name__ == "__main__":
    migrate()
