from app import app, init_db_and_seed, db
from sqlalchemy import text

if __name__ == "__main__":
    with app.app_context():
        print("Running database migration and seeding...")
        init_db_and_seed()
        
        # Explicitly migrate columns to be nullable for MySQL
        print("Migrating schema to make name/place nullable...")
        try:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE activity MODIFY name VARCHAR(100) NULL"))
                conn.execute(text("ALTER TABLE activity MODIFY place VARCHAR(200) NULL"))
                conn.commit()
            print("Schema migration successful.")
        except Exception as e:
            print(f"Migration step warning (might already be nullable): {e}")

        print("Done.")
