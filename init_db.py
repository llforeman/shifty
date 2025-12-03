#!/usr/bin/env python
"""
Database initialization script for Render deployment.
Run this once after deploying to create all tables.
"""
from app import app, db, init_db_and_seed

if __name__ == '__main__':
    with app.app_context():
        print("Creating database tables...")
        db.create_all()
        print("Tables created successfully!")
        
        print("Seeding initial data...")
        init_db_and_seed()
        print("Initialization complete!")
