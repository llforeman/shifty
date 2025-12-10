import pymysql

# Connection details from .env
# SQLALCHEMY_DATABASE_URI=mysql+pymysql://u917189230_user:Sm-or-ds1@srv1429.hstgr.io:3306/u917189230_db
HOST = 'srv1429.hstgr.io'
USER = 'u917189230_user'
PASS = 'Sm-or-ds1'
DB_NAME = 'u917189230_db'

def run_migration():
    try:
        connection = pymysql.connect(host=HOST,
                                     user=USER,
                                     password=PASS,
                                     database=DB_NAME,
                                     cursorclass=pymysql.cursors.DictCursor)
        
        with connection:
            print("Connected to MySQL.")
            with connection.cursor() as cursor:
                # Add min_staff
                try:
                    sql = "ALTER TABLE activity_type ADD COLUMN min_staff INT"
                    cursor.execute(sql)
                    print("Added min_staff column.")
                except Exception as e:
                    print(f"min_staff error: {e}")

                # Add max_staff
                try:
                    sql = "ALTER TABLE activity_type ADD COLUMN max_staff INT"
                    cursor.execute(sql)
                    print("Added max_staff column.")
                except Exception as e:
                    print(f"max_staff error: {e}")

            connection.commit()
            print("Migration committed.")
            
    except Exception as e:
        print(f"Connection failed: {e}")

if __name__ == '__main__':
    run_migration()
