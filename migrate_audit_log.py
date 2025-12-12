import sqlite3
import os
from app import app, db

def migrate():
    with app.app_context():
        print("Running migration: Create audit_log table...")
        
        try:
            with db.engine.connect() as conn:
                trans = conn.begin()
                try:
                    # Create AuditLog table
                    # Fields: id, user_id, action, target_type, target_id, timestamp, details
                    create_sql = """
                    CREATE TABLE IF NOT EXISTS audit_log (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        action VARCHAR(50) NOT NULL,
                        target_type VARCHAR(50) NOT NULL,
                        target_id INTEGER,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        details TEXT,
                        FOREIGN KEY(user_id) REFERENCES user(id)
                    );
                    """
                    conn.execute(db.text(create_sql))
                    print("Table 'audit_log' created successfully.")
                    
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
