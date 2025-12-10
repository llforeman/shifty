from app import app, db
from sqlalchemy import text

def migrate():
    with app.app_context():
        try:
            conn = db.engine.connect()
            trans = conn.begin()
            
            # Add min_staff
            try:
                conn.execute(text("ALTER TABLE activity_type ADD COLUMN min_staff INTEGER"))
                print("Added min_staff column.")
            except Exception as e:
                print(f"min_staff error (likely exists): {e}")

            # Add max_staff
            try:
                conn.execute(text("ALTER TABLE activity_type ADD COLUMN max_staff INTEGER"))
                print("Added max_staff column.")
            except Exception as e:
                print(f"max_staff error (likely exists): {e}")

            trans.commit()
            conn.close()
            print("Migration finished.")
        except Exception as e:
            print(f"Migration failed: {e}")

if __name__ == '__main__':
    migrate()
