from app import app, db
from sqlalchemy import text

with app.app_context():
    try:
        with db.engine.connect() as conn:
            # Add email column
            try:
                conn.execute(text(
                    "ALTER TABLE user ADD COLUMN email VARCHAR(120) NULL"
                ))
                conn.execute(text(
                    "CREATE UNIQUE INDEX ix_user_email ON user (email)"
                ))
                conn.commit()
                print("✅ Successfully added 'email' column and index to user table")
            except Exception as e:
                # SQLite doesn't have "IF NOT EXISTS" for columns easily, so catch duplicate
                if "duplicate column" in str(e).lower():
                    print("ℹ️  Column 'email' already exists, skipping...")
                else:
                    print(f"⚠️ Error adding email: {e}")

            # Add must_change_password column
            try:
                conn.execute(text(
                    "ALTER TABLE user ADD COLUMN must_change_password BOOLEAN DEFAULT 0"
                ))
                conn.commit()
                print("✅ Successfully added 'must_change_password' column to user table")
            except Exception as e:
                if "duplicate column" in str(e).lower():
                    print("ℹ️  Column 'must_change_password' already exists, skipping...")
                else:
                    print(f"⚠️ Error adding must_change_password: {e}")

    except Exception as e:
        print(f"❌ Error during migration: {e}")
        raise
