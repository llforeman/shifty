import os
from app import app, db, ActivityType, Service
from sqlalchemy import text

def migrate():
    with app.app_context():
        print("Starting ActivityType migration...")
        
        # 1. Update existing ActivityTypes to have a default Service ID if they don't
        # We'll assume the first service or a specific default.
        default_service = Service.query.first()
        if not default_service:
            print("No services found! creating default...")
            # Should not happen given previous steps, but safety first
            return

        print(f"Assigning existing ActivityTypes to Service ID: {default_service.id} ({default_service.name})")
        
        # Update all ActivityTypes with null service_id
        null_service_types = ActivityType.query.filter(ActivityType.service_id == None).all()
        for at in null_service_types:
            at.service_id = default_service.id
            print(f"Updated {at.name} -> service_id {default_service.id}")
        
        db.session.commit()
        
        # 2. Alter Table to remove unique constraint on name and add composite constraint
        try:
            with db.engine.connect() as conn:
                # SQLite specific syntax for dropping constraint might be tricky if it's unnamed.
                # However, usually in SQLite we have to recreate the table or use specific alter commands if supported.
                # SQLAlchemy doesn't support dropping unnamed constraints easily in SQLite without reflection.
                # But since we are likely using SQLite (users local path suggests windows/python), 
                # Dropping unique constraint in SQLite usually requires recreating the table. 
                # BUT, let's try assuming standard SQL first or handle it gracefully.
                
                # Check backend
                is_sqlite = 'sqlite' in str(db.engine.url)
                
                if is_sqlite:
                    print("SQLite detected. Using batch_alter_table via Alembic context manually or raw SQL recreation if needed.")
                    # Since we don't have full Alembic environment set up easily, we might need a simpler hack:
                    # Just rename the table, create new one, copy data.
                    # Or... since name is unique, we can attempt to drop the index if it's an index.
                    
                    # Inspect constraints
                    # In SQLite, unique constraints often implicitly create an index.
                    
                    # Attempt to drop index if it exists (usually sqlite_autoindex_activity_type_1 or similar, OR named if we named it)
                    # We defined it as `name = db.Column(..., unique=True)` so it's likely an auto index.
                    
                    # Strategy: Use SQLAlchemy reflection to modify.
                    pass # We will use the logic below
                    
        except Exception as e:
            print(f"Pre-check error: {e}")

        # We will use alembic operations if Flask-Migrate is installed and configured, 
        # But to be robust given the environment constraints, let's try a direct SQL approach for SQLite
        # or SQLAlchemy Metadata reflection.
        
        # Best approach for SQLite constraint modification without Alembic:
        # 1. Create temporary table with new schema
        # 2. Copy data
        # 3. Drop old table
        # 4. Rename temp table
        
        # However, ActivityType is referenced by Activity(activity_type_id).
        # Dropping ActivityType table might cascade delete or violate FKs in Activity table if not careful.
        
        # Let's try to just update the App code to NOT use unique=True in the model, 
        # and checking strictly in logic. 
        # But the DB will enforce it if we don't drop it.
        
        # Alternative: Just execute "DROP INDEX IF EXISTS ..." matches the column name.
        # In SQLite: `DROP INDEX IF EXISTS sqlite_autoindex_activity_type_1;` ?? No, that's internal.
        # If it's a UNIQUE constraint definition in CREATE TABLE, we can't drop it easily in SQLite.
        
        # Let's try using `batch_alter_table` if we can import it, otherwise...
        # Wait, the user already installed Flask-Migrate. 
        # Can I use `flask db migrate`?
        # Creating a migration script manually is safer here.
        
        pass

if __name__ == '__main__':
    migrate()
