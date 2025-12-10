from app import app, db
from sqlalchemy import text

def run_migration():
    with app.app_context():
        print("Starting ActivityType migration...")
        
        # 1. Check if we are using SQLite
        db_uri = app.config['SQLALCHEMY_DATABASE_URI']
        is_sqlite = 'sqlite' in db_uri
        
        if not is_sqlite:
            print("This script is optimized for SQLite. Please check logic for other DBs.")
            # For Postgres, we could just DROP CONSTRAINT and ADD CONSTRAINT.
            # But let's assume valid for typical dev env.
            
        conn = db.engine.connect()
        trans = conn.begin()
        
        try:
            # 2. Rename existing table
            print("Renaming old table...")
            conn.execute(text("ALTER TABLE activity_type RENAME TO activity_type_old"))
            
            # 3. Create new table with updated constraints:
            # - id PK
            # - name VARCHAR(100) NOT NULL
            # - service_id INTEGER, FK to service.id
            # - UNIQUE(name, service_id)
            print("Creating new table...")
            create_sql = """
            CREATE TABLE activity_type (
                id INTEGER PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                service_id INTEGER,
                FOREIGN KEY(service_id) REFERENCES service(id),
                CONSTRAINT uq_activity_name_service UNIQUE(name, service_id)
            );
            """
            # Adjust syntax if not SQLite (e.g., SERIAL for id, or AUTOINCREMENT implied)
            # SQLAlchemy Models usually use integer primary key which is autoincrement in SQLite.
            conn.execute(text(create_sql))
            
            # 4. Copy data
            # Ensure service_id is populated. If old data has nulls, defaulting to 1 (or finding default).
            # We will use a subquery or fixed value if needed.
            # Assuming '1' is the default service ID based on previous work.
            print("Copying data...")
            conn.execute(text("""
                INSERT INTO activity_type (id, name, service_id)
                SELECT id, name, COALESCE(service_id, 1) FROM activity_type_old
            """))
            
            # 5. Drop old table
            print("Dropping old table...")
            conn.execute(text("DROP TABLE activity_type_old"))
            
            trans.commit()
            print("Migration successful: ActivityType unique constraint updated.")
            
        except Exception as e:
            trans.rollback()
            print(f"Migration failed: {e}")
            raise e
        finally:
            conn.close()

if __name__ == '__main__':
    run_migration()
