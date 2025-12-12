import os
import subprocess 
import boto3
from datetime import datetime
import sys

# Requirements: pip install boto3

def backup_and_upload():
    print("Starting Backup Process...")

    # 1. Configuration
    db_url = os.getenv('DATABASE_URL') or os.getenv('SQLALCHEMY_DATABASE_URI')
    aws_access_key = os.getenv('AWS_ACCESS_KEY_ID')
    aws_secret_key = os.getenv('AWS_SECRET_ACCESS_KEY')
    s3_bucket = os.getenv('AWS_S3_BUCKET_NAME')
    s3_region = os.getenv('AWS_S3_REGION', 'eu-west-3') # Default to Paris
    
    if not all([db_url, aws_access_key, aws_secret_key, s3_bucket]):
        print("ERROR: Missing required environment variables.")
        print("Ensure DATABASE_URL, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, and AWS_S3_BUCKET_NAME are set.")
        sys.exit(1)

    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    backup_filename = f"backup_{timestamp}.sql"
    
    # 2. CREATE BACKUP
    print(f"Creating dump: {backup_filename}")
    
    if db_url.startswith('sqlite'):
        print("Detected SQLite. Copying file...")
        # For SQLite, db_url is typically sqlite:///app.db
        db_path = db_url.replace('sqlite:///', '')
        if not os.path.exists(db_path):
             # Try absolute path or just 'instance/app.db'?
             # Flask default is instance/ped_schedule.db or similar.
             # We'll just define it explicitly if needed, but for now fallback to cp
             db_path = "instance/ped_scheduler.db" 
             if not os.path.exists(db_path):
                  db_path = "ped_scheduler.db"

        if os.path.exists(db_path):
             subprocess.run (f"copy {db_path} {backup_filename}", shell=True, check=True) # Windows/Linux diffs handling
             # Actually better to use python shutil
             import shutil
             shutil.copy2(db_path, backup_filename)
        else:
             print(f"Error: SQLite DB not found at {db_path}")
             sys.exit(1)
             
    else: 
        # Postgres (Production)
        # Handle 'postgres://' vs 'postgresql://' (SQLAlchemy needs postgresql, pg_dump needs postgres or just uri)
        # Render provides DATABASE_URL.
        try:
            # We use pg_dump. 
            # Note: Render python environment might not have pg_dump installed by default unless we add a buildpack.
            # But typically we can install it.
            # Command: pg_dump $DATABASE_URL > filename
            env = os.environ.copy()
            # If password is in URL, pg_dump handles it.
            
            subprocess.run(f"pg_dump {db_url} -F c -f {backup_filename}", shell=True, check=True)
            # -F c (Custom format, compressed)
            print("Postgres dump successful.")
            
        except subprocess.CalledProcessError as e:
            print(f"Error running pg_dump: {e}")
            sys.exit(1)

    # 3. UPLOAD TO S3
    print(f"Uploading to S3 Bucket: {s3_bucket} ({s3_region})...")
    
    try:
        s3 = boto3.client(
            's3',
            aws_access_key_id=aws_access_key,
            aws_secret_access_key=aws_secret_key,
            region_name=s3_region
        )
        
        # Upload
        s3.upload_file(backup_filename, s3_bucket, f"backups/{backup_filename}")
        print("Upload Successful!")
        
    except Exception as e:
        print(f"S3 Upload Error: {e}")
        sys.exit(1)
        
    # 4. CLEANUP
    if os.path.exists(backup_filename):
        os.remove(backup_filename)
        print("Local backup file cleaned up.")
        
    print("Backup Process Completed Successfully.")

if __name__ == "__main__":
    backup_and_upload()
