from app import app, init_db_and_seed

if __name__ == "__main__":
    with app.app_context():
        print("Running database migration and seeding...")
        init_db_and_seed()
        print("Done.")
