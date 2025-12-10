from app import app, db, Organization, Service, Pediatrician, User, GlobalConfig, ActivityType
import sqlalchemy as sa
from sqlalchemy import text

def run_migration():
    with app.app_context():
        print("🚀 Starting Multi-tenancy Migration...")
        
        # 1. Create new tables
        # We can use db.create_all() but it might try to recreate existing tables.
        # Ideally we check existence. But safely, we can just create_all since it checks existence.
        print("Creating tables...")
        db.create_all()
        
        # 2. Seed Default Organization and Service
        print("Seeding default Service...")
        org = Organization.query.filter_by(name='Hospital General').first()
        if not org:
            org = Organization(name='Hospital General')
            db.session.add(org)
            db.session.commit()
            print(f"✅ Created Organization: {org.name}")
            
        svc = Service.query.filter_by(name='Pediatría', organization_id=org.id).first()
        if not svc:
            svc = Service(name='Pediatría', organization_id=org.id)
            db.session.add(svc)
            db.session.commit()
            print(f"✅ Created Service: {svc.name}")
        
        default_service_id = svc.id
        
        # 3. Add Columns (Manually if NOT using Alembic/Flask-Migrate, which we are not properly using yet)
        # Check if columns exist using raw SQL, then add them.
        # SQLite doesn't support IF NOT EXISTS in ALTER TABLE well, so we try-except.
        
        conn = db.session.connection()
        
        tables_to_migrate = [
            ('pediatrician', 'service_id'),
            ('user', 'active_service_id'),
            ('activity_type', 'service_id'),
            ('global_config', 'service_id')
        ]
        
        for table, column in tables_to_migrate:
            try:
                print(f"Adding {column} to {table}...")
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER REFERENCES service(id)"))
                print(f"✅ Added {column} to {table}")
            except Exception as e:
                # Column likely exists
                print(f"ℹ️  Column {column} likely exists in {table} (or error: {e})")

        # 4. Migrate Data (Set defaults)
        print("Migrating data to default service...")
        
        # Pediatrician
        # Using raw SQL for bulk update speed and simplicity
        conn.execute(text(f"UPDATE pediatrician SET service_id = :sid WHERE service_id IS NULL"), {'sid': default_service_id})
        
        # User
        conn.execute(text(f"UPDATE user SET active_service_id = :sid WHERE active_service_id IS NULL"), {'sid': default_service_id})
        
        # ActivityType
        conn.execute(text(f"UPDATE activity_type SET service_id = :sid WHERE service_id IS NULL"), {'sid': default_service_id})
        
        # GlobalConfig
        conn.execute(text(f"UPDATE global_config SET service_id = :sid WHERE service_id IS NULL"), {'sid': default_service_id})
        
        db.session.commit()
        print("✅ Data migration complete.")
        
        # 5. Handle Unique Constraints (Complex in SQLite)
        # We changed GlobalConfig.key unique to (key, service_id).
        # We changed Pediatrician.name unique to potentially non-unique (globally).
        # In SQLite, altering constraints requires table rebuild. We will skip this for now safely
        # as the existing data is valid. Ideally we would rebuild tables.
        
        print("\n⚠️  NOTE: SQLite Constraints were NOT fully updated (requires table rebuild).")
        print("However, the schema now supports the 'service_id' columns.")
        print("The application code will enforce filtering.")

if __name__ == "__main__":
    run_migration()
