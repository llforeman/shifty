import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'ped_scheduler.db')

def run_migration():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        # Add min_staff column
        try:
            cursor.execute("ALTER TABLE activity_type ADD COLUMN min_staff INTEGER")
            print("Added min_staff column.")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                print("min_staff column already exists.")
            else:
                raise e

        # Add max_staff column
        try:
            cursor.execute("ALTER TABLE activity_type ADD COLUMN max_staff INTEGER")
            print("Added max_staff column.")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                print("max_staff column already exists.")
            else:
                raise e
                
        conn.commit()
        print("Migration successful.")
        
    except Exception as e:
        print(f"Migration failed: {e}")
        conn.rollback()
    finally:
        conn.close()

if __name__ == '__main__':
    run_migration()
