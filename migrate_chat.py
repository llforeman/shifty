
import os
from sqlalchemy import create_engine, text

# Get database URL from environment (same as app.py)
DATABASE_URL = os.environ.get('DATABASE_URL') or 'sqlite:///ped_scheduler.db'
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

def migrate_db():
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        # Check if column exists (MySQL specific check or generic try/catch)
        try:
            print("Attempting to add recipient_id column to chat_message table...")
            # MySQL syntax
            conn.execute(text("ALTER TABLE chat_message ADD COLUMN recipient_id INTEGER"))
            conn.execute(text("ALTER TABLE chat_message ADD CONSTRAINT fk_chat_recipient FOREIGN KEY (recipient_id) REFERENCES user(id)"))
            print("Migration successful.")
            conn.commit()
        except Exception as e:
            print(f"Migration might have failed or column already exists: {e}")

if __name__ == "__main__":
    migrate_db()
