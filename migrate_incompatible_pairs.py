from app import app, db
from sqlalchemy import text

def migrate():
    with app.app_context():
        # Check if table exists
        inspector = db.inspect(db.engine)
        tables = inspector.get_table_names()
        
        if 'incompatible_pair' not in tables:
            print("Creating 'incompatible_pair' table...")
            # Create the table using SQLAlchemy's metadata
            # We import the model so it's registered
            from app import IncompatiblePair
            IncompatiblePair.__table__.create(db.engine)
            print("Table 'incompatible_pair' created successfully.")
        else:
            print("Table 'incompatible_pair' already exists.")

if __name__ == '__main__':
    migrate()
