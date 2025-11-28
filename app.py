from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from datetime import date
import os
from dotenv import load_dotenv

load_dotenv()

# --- CONFIGURATION ---
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('SQLALCHEMY_DATABASE_URI')
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# -----------------
# 1. DATABASE MODELS (Defining the tables)
# -----------------
class Pediatrician(db.Model):
    __tablename__ = 'pediatrician'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    # These fields replace the limits previously read from the top of the Excel sheet
    min_shifts = db.Column(db.Integer, default=1)
    max_shifts = db.Column(db.Integer, default=5)
    min_weekend = db.Column(db.Integer, default=0)
    max_weekend = db.Column(db.Integer, default=2)
    type = db.Column(db.String(50))
    mir = db.Column(db.Boolean, default=False)

    preferences = db.relationship('Preference', backref='pediatrician', lazy=True)
    
    def __repr__(self):
        return f"<Pediatrician {self.name}>"

class Preference(db.Model):
    __tablename__ = 'preference'
    
    id = db.Column(db.Integer, primary_key=True)
    pediatrician_id = db.Column(db.Integer, db.ForeignKey('pediatrician.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    # Stores the type of request: 'Vacation', 'Skip', 'Prefer Not', 'Prefer'
    type = db.Column(db.String(50), nullable=False)
    
    # Constraint: A pediatrician can only have one request per date
    __table_args__ = (db.UniqueConstraint('pediatrician_id', 'date', name='_ped_date_uc'),)

    def __repr__(self):
        return f"<Preference {self.pediatrician.name} on {self.date} for {self.type}>"

class GlobalConfig(db.Model):
    __tablename__ = 'global_config'
    
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.String(255), nullable=False)

    def __repr__(self):
        return f"<GlobalConfig {self.key}={self.value}>"

# -----------------
# 2. DATABASE INITIALIZATION (Run this once to create tables)
# -----------------
def seed_global_config():
    """Seeds the database with default configuration values."""
    defaults = {
        'S1': '2', # min pediatricians/day
        'S2': '2', # max pediatricians/day
        'M_START': '3', # Días de solapamiento inicial
        'M_MIN': '1', # Días de solapamiento mínimo
        'BALANCE_ALPHA': '1.0',
        'USE_LEXICOGRAPHIC_FAIRNESS': 'True',
        'PENALTY_PREFER_NOT_DAY': '10',
        'PENALTY_MISS_PREFERRED_DAY': '8',
        'PENALTY_EXCESS_WEEKLY_SHIFTS': '5',
        'PENALTY_REPEATED_WEEKDAY': '30',
        'PENALTY_REPEATED_PAIRING': '35',
        'PENALTY_MONTHLY_BALANCE': '60',
        'PENALTY_SHIFT_LIMIT_VIOLATION': '500',
        'PENALTY_WEEKEND_LIMIT_VIOLATION': '400',
    }
    
    for key, value in defaults.items():
        if not GlobalConfig.query.filter_by(key=key).first():
            db.session.add(GlobalConfig(key=key, value=value))
    db.session.commit()
    print("Seeded GlobalConfig with default values.")

def init_db_and_seed():
    """Creates tables and adds initial test data if none exists."""
    with app.app_context():
        # Creates all tables defined by the Models
        db.create_all()
        print("Database tables created.")
        
        # Seed global config
        seed_global_config()
        
        # Add a test pediatrician if the table is empty
        if Pediatrician.query.count() == 0:
            test_ped = Pediatrician(id=1, name="Dr. Test User", min_shifts=3, max_shifts=6, min_weekend=1, max_weekend=2)
            db.session.add(test_ped)
            db.session.commit()
            print("Seeded database with one test user (Dr. Test User, ID=1).")

# -----------------
# 3. WEB ROUTES (The logic that serves the pages)
# -----------------
# We will use this route in the next step to view and submit preferences
# For now, it requires a ped_id (e.g., /prefs/1) because we haven't implemented login yet.
@app.route('/prefs/<int:ped_id>', methods=['GET', 'POST'])
def preferences_page(ped_id):
    pediatrician = db.get_or_404(Pediatrician, ped_id)
    
    if request.method == 'POST':
        req_date_str = request.form.get('request_date')
        req_type = request.form.get('request_type')

        if req_date_str and req_type:
            try:
                # Convert string to Python date object
                req_date = date.fromisoformat(req_date_str)
                
                # Check for existing preference for update/delete
                existing_entry = Preference.query.filter_by(
                    pediatrician_id=ped_id, date=req_date
                ).first()

                if req_type == 'Delete':
                    if existing_entry:
                        db.session.delete(existing_entry)
                elif existing_entry:
                    existing_entry.type = req_type
                else:
                    new_pref = Preference(pediatrician_id=ped_id, date=req_date, type=req_type)
                    db.session.add(new_pref)
                
                db.session.commit()
                return redirect(url_for('preferences_page', ped_id=ped_id))

            except ValueError as e:
                print(f"Error processing date: {e}")
                # In a real app, you'd show an error message to the user
                
    # Fetch all current preferences to display on the page
    existing_prefs = Preference.query.filter_by(pediatrician_id=ped_id).order_by(Preference.date).all()
        
    return render_template(
        'preferences_form.html',
        pediatrician=pediatrician,
        existing_prefs=existing_prefs
    )

@app.route('/manager_config', methods=['GET', 'POST'])
def manager_config():
    # In a real app, you would add authentication here (e.g. @login_required)
    
    if request.method == 'POST':
        # Update values from form
        for key, value in request.form.items():
            # Skip the submit button or other non-config fields if any
            if key != 'submit':
                config_item = GlobalConfig.query.filter_by(key=key).first()
                if config_item:
                    config_item.value = value
        db.session.commit()
        return redirect(url_for('manager_config'))
    
    # Fetch all configs
    config_items = GlobalConfig.query.all()
    # Convert to dictionary for easier access in template
    config_dict = {item.key: item.value for item in config_items}
    
    return render_template('manager_config.html', config=config_dict)


if __name__ == '__main__':
    # Initialize database before running the app
    init_db_and_seed()
    # The debug=True line should be removed for final deployment
    app.run(debug=True)
