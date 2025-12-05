from app import app, db
from sqlalchemy import text

with app.app_context():
    try:
        # Add column if it doesn't exist
        with db.engine.connect() as conn:
            # Try to add the column
            try:
                conn.execute(text(
                    "ALTER TABLE preference ADD COLUMN recurring_group VARCHAR(150) NULL"
                ))
                conn.commit()
                print("✅ Successfully added 'recurring_group' column to preference table")
            except Exception as e:
                if "Duplicate column name" in str(e) or "duplicate column" in str(e).lower():
                    print("ℹ️  Column 'recurring_group' already exists, skipping...")
                else:
                    raise e
    except Exception as e:
        print(f"❌ Error during migration: {e}")
        raise
