from flask import Flask, render_template, request, redirect, url_for, abort, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import date, datetime
import calendar
import os
from dotenv import load_dotenv
from functools import wraps
from redis import Redis
from rq import Queue
from rq.job import Job

load_dotenv()

# --- CONFIGURATION ---
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('SQLALCHEMY_DATABASE_URI')
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# Prevent MySQL "server has gone away" errors
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,  # Test connections before using them
    'pool_recycle': 900,    # Recycle connections after 15 min (< MySQL wait_timeout)
}
db = SQLAlchemy(app)

# Redis and RQ configuration
redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')
redis_conn = Redis.from_url(redis_url)
task_queue = Queue('default', connection=redis_conn)
# Configure session cookies
# Always use secure cookies in production (Render uses HTTPS)
# The ENVIRONMENT variable might not be set, so we also check for typical production indicators
is_production = (
    os.getenv('ENVIRONMENT') == 'production' or 
    os.getenv('RENDER') == 'true' or  # Render sets this automatically
    os.getenv('SQLALCHEMY_DATABASE_URI', '').startswith('mysql://')  # Production DB
)

if is_production:
    # Cross-domain cookie support: Required when app is embedded or accessed from different domain
    # SameSite=None allows cookies to be sent cross-site (xifty.org -> shifty.onrender.com)
    # Secure=True is required when using SameSite=None (HTTPS only)
    app.config['SESSION_COOKIE_SAMESITE'] = 'None'
    app.config['SESSION_COOKIE_SECURE'] = True
    print("[DEBUG] Production mode - using SameSite=None for cross-domain cookie support")
else:
    # Development/local: use standard session cookies
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_SECURE'] = False
    
app.config['SESSION_COOKIE_HTTPONLY'] = True  # Security best practice
app.config['PERMANENT_SESSION_LIFETIME'] = 86400  # 24 hours

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

def role_required(*roles):
    def wrapper(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('login'))
            if current_user.role not in roles:
                abort(403)
            return f(*args, **kwargs)
        return decorated_function
    return wrapper

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

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True)
    password_hash = db.Column(db.String(255))
    role = db.Column(db.String(50), default='user') # 'manager' or 'user'
    pediatrician_id = db.Column(db.Integer, db.ForeignKey('pediatrician.id'), nullable=True) # Null for managers
    
    # Relationship to access pediatrician data
    pediatrician = db.relationship('Pediatrician', backref='users', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

@login_manager.user_loader
def load_user(user_id):
    print(f"[DEBUG] user_loader called with ID: {user_id}")
    user = db.session.get(User, int(user_id))
    print(f"[DEBUG] user_loader found: {user}")
    return user

class Preference(db.Model):
    __tablename__ = 'preference'
    
    id = db.Column(db.Integer, primary_key=True)
    pediatrician_id = db.Column(db.Integer, db.ForeignKey('pediatrician.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    # Stores the type of request: 'Vacation', 'Skip', 'Prefer Not', 'Prefer'
    type = db.Column(db.String(50), nullable=False)
    # For recurring preferences: e.g., "tuesday_prefernot_202601_202606" (null for individual dates)
    recurring_group = db.Column(db.String(150), nullable=True)
    
    # Constraint: A pediatrician can only have one request per date
    __table_args__ = (db.UniqueConstraint('pediatrician_id', 'date', name='_ped_date_uc'),)

    def __repr__(self):
        return f"<Preference {self.pediatrician.name} on {self.date} for {self.type}>"

class Shift(db.Model):
    __tablename__ = 'shift'
    
    id = db.Column(db.Integer, primary_key=True)
    pediatrician_id = db.Column(db.Integer, db.ForeignKey('pediatrician.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    type = db.Column(db.String(50), default='Shift') # 'Shift', 'Guardia', etc.
    
    # Relationship to access the pediatrician
    pediatrician = db.relationship('Pediatrician', backref='shifts', lazy=True)
    
    # Constraint: A pediatrician can only have one shift per date (usually)
    __table_args__ = (db.UniqueConstraint('pediatrician_id', 'date', name='_ped_shift_uc'),)

    def __repr__(self):
        return f"<Shift {self.pediatrician_id} on {self.date}>"

class DraftShift(db.Model):
    __tablename__ = 'draft_shift'
    
    id = db.Column(db.Integer, primary_key=True)
    pediatrician_id = db.Column(db.Integer, db.ForeignKey('pediatrician.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    type = db.Column(db.String(50), default='Shift') # 'Shift', 'Guardia', etc.
    
    # Relationship to access the pediatrician
    pediatrician = db.relationship('Pediatrician', backref='draft_shifts', lazy=True)
    
    # Constraint: A pediatrician can only have one shift per date (usually)
    __table_args__ = (db.UniqueConstraint('pediatrician_id', 'date', name='_ped_draft_shift_uc'),)

    def __repr__(self):
        return f"<DraftShift {self.pediatrician_id} on {self.date}>"

class ShiftSwapRequest(db.Model):
    __tablename__ = 'shift_swap_request'
    
    id = db.Column(db.Integer, primary_key=True)
    requester_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    # The shift the requester wants to GIVE UP
    requester_shift_id = db.Column(db.Integer, db.ForeignKey('shift.id'), nullable=False)
    
    target_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    # The shift the requester wants to TAKE (or None if just giving up? No, requirements say swap)
    target_shift_id = db.Column(db.Integer, db.ForeignKey('shift.id'), nullable=False)
    
    # Status: 'pending_peer', 'pending_admin', 'approved', 'rejected'
    status = db.Column(db.String(50), default='pending_peer')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    requester = db.relationship('User', foreign_keys=[requester_id], backref='sent_swaps')
    target_user = db.relationship('User', foreign_keys=[target_user_id], backref='received_swaps')
    requester_shift = db.relationship('Shift', foreign_keys=[requester_shift_id])
    target_shift = db.relationship('Shift', foreign_keys=[target_shift_id])

class Notification(db.Model):
    __tablename__ = 'notification'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    message = db.Column(db.String(255), nullable=False)
    link = db.Column(db.String(255)) # e.g., /notifications
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    user = db.relationship('User', backref='notifications')

    def __repr__(self):
        return f"<Notification {self.user_id}: {self.message}>"

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

            # Create a login user for this pediatrician
            if not User.query.filter_by(username='dr_test').first():
                user = User(username='dr_test', role='user', pediatrician_id=test_ped.id)
                user.set_password('password')
                db.session.add(user)
                db.session.commit()
                print("Created test user (dr_test/password) linked to Dr. Test User")

        # Create default admin user if not exists
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', role='manager')
            admin.set_password('admin123') # Change this in production!
            db.session.add(admin)
            db.session.commit()
            print("Created default admin user (admin/admin123)")

# Initialize database when app starts (within proper context)
with app.app_context():
    try:
        db.create_all()
        print("Database tables created.")
    except Exception as e:
        print(f"Database initialization skipped: {e}")



# Helper function to expand weekday to all dates in a range
def expand_weekday_to_dates(weekday_name, start_month, start_year, end_month, end_year):
    """
    Expand a weekday (e.g., 'Monday') to all dates of that weekday within the given month range.
    Returns list of date objects.
    """
    from datetime import timedelta
    
    weekday_map = {
        'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
        'friday': 4, 'saturday': 5, 'sunday': 6
    }
    
    weekday_num = weekday_map.get(weekday_name.lower())
    if weekday_num is None:
        return []
    
    dates = []
    start_date = date(start_year, start_month, 1)
    
    # Calculate end date (last day of end month)
    if end_month == 12:
        end_date = date(end_year, 12, 31)
    else:
        end_date = date(end_year, end_month + 1, 1) - timedelta(days=1)
    
    # Find first occurrence of the weekday
    current = start_date
    while current.weekday() != weekday_num:
        current += timedelta(days=1)
        if current > end_date:
            return dates
    
    # Collect all dates of this weekday in range
    while current <= end_date:
        dates.append(current)
        current += timedelta(days=7)
    
    return dates

# -----------------
# 3. WEB ROUTES (The logic that serves the pages)
# -----------------
# We will use this route in the next step to view and submit preferences
# For now, it requires a ped_id (e.g., /prefs/1) because we haven't implemented login yet.
@app.route('/prefs/<int:ped_id>', methods=['GET', 'POST'])
@login_required
def preferences_page(ped_id):
    # RBAC: Only allow if user is manager OR if user owns this pediatrician_id
    if current_user.role != 'manager' and current_user.pediatrician_id != ped_id:
        abort(403) # Forbidden

    pediatrician = db.get_or_404(Pediatrician, ped_id)
    
    if request.method == 'POST':
        preference_mode = request.form.get('preference_mode')  # 'specific' or 'recurring'
        req_type = request.form.get('request_type')
        
        # Handle deletion of recurring group
        if request.form.get('delete_recurring_group'):
            recurring_group = request.form.get('delete_recurring_group')
            Preference.query.filter_by(
                pediatrician_id=ped_id,
                recurring_group=recurring_group
            ).delete()
            db.session.commit()
            return redirect(url_for('preferences_page', ped_id=ped_id))
        
        # Handle specific date preference
        if preference_mode == 'specific':
            req_date_str = request.form.get('request_date')
            
            if req_date_str and req_type:
                try:
                    req_date = date.fromisoformat(req_date_str)
                    
                    existing_entry = Preference.query.filter_by(
                        pediatrician_id=ped_id, date=req_date
                    ).first()

                    if req_type == 'Delete':
                        if existing_entry:
                            db.session.delete(existing_entry)
                    elif existing_entry:
                        existing_entry.type = req_type
                        existing_entry.recurring_group = None  # Clear any recurring group
                    else:
                        new_pref = Preference(
                            pediatrician_id=ped_id, 
                            date=req_date, 
                            type=req_type,
                            recurring_group=None
                        )
                        db.session.add(new_pref)
                    
                    db.session.commit()
                    return redirect(url_for('preferences_page', ped_id=ped_id))
                except ValueError as e:
                    print(f"Error processing date: {e}")
        
        # Handle recurring weekday preference
        elif preference_mode == 'recurring':
            weekday = request.form.get('weekday')
            start_month = request.form.get('start_month')
            start_year = request.form.get('start_year')
            end_month = request.form.get('end_month')
            end_year = request.form.get('end_year')
            
            if weekday and start_month and start_year and end_month and end_year and req_type:
                try:
                    # Generate recurring group identifier
                    recurring_group = f"{weekday.lower()}_{req_type.replace(' ', '').lower()}_{start_year}{start_month.zfill(2)}_{end_year}{end_month.zfill(2)}"
                    
                    # Expand weekday to all dates
                    dates_to_add = expand_weekday_to_dates(
                        weekday, 
                        int(start_month), int(start_year),
                        int(end_month), int(end_year)
                    )
                    
                    # Add all dates with the same recurring_group
                    for pref_date in dates_to_add:
                        # Check if preference already exists for this date
                        existing = Preference.query.filter_by(
                            pediatrician_id=ped_id,
                            date=pref_date
                        ).first()
                        
                        if not existing:
                            new_pref = Preference(
                                pediatrician_id=ped_id,
                                date=pref_date,
                                type=req_type,
                                recurring_group=recurring_group
                            )
                            db.session.add(new_pref)
                    
                    db.session.commit()
                    return redirect(url_for('preferences_page', ped_id=ped_id))
                except Exception as e:
                    print(f"Error processing recurring preference: {e}")
                    db.session.rollback()
                
    # Fetch and group preferences for display
    all_prefs = Preference.query.filter_by(pediatrician_id=ped_id).order_by(Preference.date).all()
    
    # Separate individual and recurring preferences
    individual_prefs = [p for p in all_prefs if p.recurring_group is None]
    
    # Group recurring preferences
    recurring_groups = {}
    for pref in all_prefs:
        if pref.recurring_group:
            if pref.recurring_group not in recurring_groups:
                recurring_groups[pref.recurring_group] = []
            recurring_groups[pref.recurring_group].append(pref)
    
    # Format recurring groups for display
    formatted_recurring = []
    for group_id, prefs in recurring_groups.items():
        if prefs:
            # Parse group_id to extract info
            # Format: "monday_prefernot_202601_202606"
            parts = group_id.split('_')
            weekday = parts[0].capitalize() if parts else "Unknown"
            
            # Map Spanish weekday names
            weekday_spanish = {
                'Monday': 'Lunes', 'Tuesday': 'Martes', 'Wednesday': 'Miércoles',
                'Thursday': 'Jueves', 'Friday': 'Viernes', 'Saturday': 'Sábado', 'Sunday': 'Domingo'
            }
            weekday_display = weekday_spanish.get(weekday, weekday)
            
            formatted_recurring.append({
                'group_id': group_id,
                'weekday': weekday_display,
                'type': prefs[0].type,
                'count': len(prefs),
                'start_date': min(p.date for p in prefs),
                'end_date': max(p.date for p in prefs)
            })
        
    return render_template(
        'preferences_form.html',
        pediatrician=pediatrician,
        individual_prefs=individual_prefs,
        recurring_prefs=formatted_recurring
    )

@app.route('/login', methods=['GET', 'POST'])
def login():
    from flask import session as flask_session
    print(f"[DEBUG] /login - Before login, session: {dict(flask_session)}")
    print(f"[DEBUG] /login - is_authenticated: {current_user.is_authenticated}")
    
    if current_user.is_authenticated:
        # Fixed: Send all users to profile, not just managers
        return redirect(url_for('profile'))
        
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            print(f"[DEBUG] /login - Login successful for user: {username}")
            login_user(user, remember=True)
            print(f"[DEBUG] /login - After login_user, session: {dict(flask_session)}")
            print(f"[DEBUG] /login - After login_user, is_authenticated: {current_user.is_authenticated}")
            next_page = request.args.get('next')
            
            if next_page:
                return redirect(next_page)
            
            # All users go to profile page after login
            return redirect(url_for('profile'))
        
        print(f"[DEBUG] /login - Login failed for user: {username}")
        return render_template('login.html', error='Usuario o contraseña inválidos')

    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('my_prefs'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        if password != confirm_password:
            return render_template('register.html', error='Las contraseñas no coinciden')
        
        if User.query.filter_by(username=username).first():
            return render_template('register.html', error='El usuario ya existe')
        
        # Create new user (default role='user', not linked to pediatrician yet)
        new_user = User(username=username, role='user')
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.commit()
        
        login_user(new_user, remember=True)
        return redirect(url_for('profile')) # Redirect to profile to see status
        
    return render_template('register.html')

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    from flask import session as flask_session
    print(f"[DEBUG] /profile - is_authenticated: {current_user.is_authenticated}")
    print(f"[DEBUG] /profile - session: {dict(flask_session)}")
    print(f"[DEBUG] /profile - current_user: {current_user}")
    
    msg = None
    msg_category = ''
    
    if request.method == 'POST':
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        
        if new_password and new_password == confirm_password:
            current_user.set_password(new_password)
            db.session.commit()
            msg = 'Contraseña actualizada correctamente.'
            msg_category = 'success'
        else:
            msg = 'Las contraseñas no coinciden.'
            msg_category = 'error'
            
    return render_template('profile.html', msg=msg, msg_category=msg_category)

@app.route('/my_prefs')
@login_required
def my_prefs():
    if current_user.role == 'manager':
        return redirect(url_for('manager_config'))
    elif current_user.pediatrician_id:
        return redirect(url_for('preferences_page', ped_id=current_user.pediatrician_id))
    else:
        return render_template('profile.html', msg='Su cuenta no está vinculada a ningún pediatra. Contacte al administrador.', msg_category='error')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/manager_config', methods=['GET', 'POST'])
@login_required
@role_required('manager')
def manager_config():
    
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


@app.route('/generate_schedule', methods=['POST'])
@login_required
@role_required('manager')
def generate_schedule_route():
    try:
        # Get date range from form
        start_year = int(request.form.get('start_year'))
        start_month = int(request.form.get('start_month'))
        end_year = int(request.form.get('end_year'))
        end_month = int(request.form.get('end_month'))
        
        # Validate date range
        start_date = datetime(start_year, start_month, 1)
        end_date = datetime(end_year, end_month, 1)
        
        if end_date < start_date:
            return redirect(url_for('manager_config', error='La fecha de fin debe ser posterior a la fecha de inicio'))
        
        # Calculate number of months
        months_diff = (end_year - start_year) * 12 + (end_month - start_month) + 1
        
        if months_diff < 1 or months_diff > 6:
            return redirect(url_for('manager_config', error='El rango debe ser de 1 a 6 meses'))
        
        # Queue the job asynchronously
        from worker import generate_schedule_task
        job = task_queue.enqueue(
            generate_schedule_task,
            start_year, start_month, end_year, end_month,
            job_timeout='30m'  # 30 minute timeout for the job
        )
        
        # Return job ID to frontend
        return jsonify({
            'status': 'queued',
            'job_id': job.id,
            'message': f'Schedule generation started for {start_year}/{start_month} to {end_year}/{end_month}'
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/job_status/<job_id>')
@login_required
@role_required('manager')
def job_status(job_id):
    """Check the status of an async job"""
    try:
        job = Job.fetch(job_id, connection=redis_conn)
        
        if job.is_finished:
            result = job.result
            return jsonify({
                'status': 'completed',
                'result': result
            })
        elif job.is_failed:
            return jsonify({
                'status': 'failed',
                'error': str(job.exc_info)
            })
        elif job.is_queued:
            return jsonify({
                'status': 'queued',
                'message': 'Job is waiting to start...'
            })
        else:  # job.is_started
            return jsonify({
                'status': 'running',
                'message': 'Schedule generation in progress...'
            })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 404


@app.route('/debug/shifts')
@login_required
@role_required('manager')
def debug_shifts():
    """Debug route to see all shifts in database"""
    all_shifts = Shift.query.order_by(Shift.date).all()
    output = f"<h1>Total Shifts: {len(all_shifts)}</h1>"
    output += "<ul>"
    for shift in all_shifts:
        output += f"<li>Pediatrician {shift.pediatrician_id} on {shift.date}</li>"
    output += "</ul>"
    return output

@app.route('/publish_schedule/<int:year>/<int:month>', methods=['POST'])
@login_required
@role_required('manager')
def publish_schedule(year, month):
    try:
        start_date = date(year, month, 1)
        _, last_day = calendar.monthrange(year, month)
        end_date = date(year, month, last_day)

        # 1. Clear existing live shifts for this range
        Shift.query.filter(Shift.date >= start_date, Shift.date <= end_date).delete()
        
        # 2. Get draft shifts
        drafts = DraftShift.query.filter(DraftShift.date >= start_date, DraftShift.date <= end_date).all()
        
        # 3. Copy to Shift table
        new_shifts = []
        for d in drafts:
            new_shifts.append(Shift(
                pediatrician_id=d.pediatrician_id, 
                date=d.date,
                type=d.type
            ))
            
        db.session.add_all(new_shifts)
        db.session.commit()
        
        return redirect(url_for('calendar_view', year=year, month=month))
    except Exception as e:
        db.session.rollback()
        return f"Error publishing schedule: {str(e)}", 500

@app.route('/calendar')
@app.route('/calendar/<int:year>/<int:month>')
@login_required
def calendar_view(year=None, month=None):
    if year is None or month is None:
        today = date.today()
        year, month = today.year, today.month
        
    # Check if we are viewing draft (manager only)
    is_draft = request.args.get('mode') == 'draft'
    if is_draft and current_user.role != 'manager':
        abort(403)

    # Calculate prev/next month for navigation
    if month == 1:
        prev_month, prev_year = 12, year - 1
    else:
        prev_month, prev_year = month - 1, year
        
    if month == 12:
        next_month, next_year = 1, year + 1
    else:
        next_month, next_year = month + 1, year
        
    # Get calendar matrix
    cal = calendar.monthcalendar(year, month)
    
    # Get shifts for this month
    start_date = date(year, month, 1)
    _, last_day = calendar.monthrange(year, month)
    end_date = date(year, month, last_day)
    
    # Select which table to query
    ModelClass = DraftShift if is_draft else Shift
    shifts_query = ModelClass.query.filter(ModelClass.date >= start_date, ModelClass.date <= end_date)
    
    # Regular users only see their own shifts (NOT applicable for draft mode as purely admin)
    if not is_draft and current_user.role != 'manager' and current_user.pediatrician_id:
        shifts_query = shifts_query.filter(ModelClass.pediatrician_id == current_user.pediatrician_id)
    
    shifts_list = shifts_query.all()
    
    # If no shifts found for this month, check for future shifts (navigation help)
    next_shift_date = None
    if not shifts_list:
        next_shift_query = ModelClass.query.filter(ModelClass.date > end_date).order_by(ModelClass.date).first()
        if next_shift_query:
            next_shift_date = next_shift_query.date

    # Organize shifts by day
    shifts_by_day = {}
    for shift in shifts_list:
        day = shift.date.day
        if day not in shifts_by_day:
            shifts_by_day[day] = []
        shifts_by_day[day].append(shift)
        
    month_name = date(year, month, 1).strftime('%B')
    
    return render_template('calendar.html', 
                           year=year, month=month, month_name=month_name,
    target_shift_id = data.get('target_shift_id')
    
    if not source_shift_id or not target_shift_id:
        return jsonify({'status': 'error', 'message': 'Missing shift IDs'}), 400

    try:
        # Verify ownership
        source_shift = db.session.get(Shift, source_shift_id)
        target_shift = db.session.get(Shift, target_shift_id)
        
        if not source_shift or not target_shift:
             return jsonify({'status': 'error', 'message': 'Shift not found'}), 404
             
        # Check if current user owns the source shift
        if current_user.pediatrician_id != source_shift.pediatrician_id:
             return jsonify({'status': 'error', 'message': 'You can only swap your own shifts'}), 403
             
        # Find target user
        target_ped_id = target_shift.pediatrician_id
        # Assuming one user per pediatrician for simplicity, or notify all users linked to that ped
        target_user = User.query.filter_by(pediatrician_id=target_ped_id).first()
        
        if not target_user:
             return jsonify({'status': 'error', 'message': 'Target pediatrician has no linked user'}), 400

        # Create Request
        swap_req = ShiftSwapRequest(
            requester_id=current_user.id,
            requester_shift_id=source_shift.id,
            target_user_id=target_user.id,
            target_shift_id=target_shift.id,
            status='pending_peer'
        )
        db.session.add(swap_req)
        
        # Create Notification for Target
        msg = f"User {current_user.username} wants to swap their shift on {source_shift.date} with your shift on {target_shift.date}."
        notif = Notification(
            user_id=target_user.id,
            message=msg,
            link=url_for('notifications_page')
        )
        db.session.add(notif)
        
        db.session.commit()
        return jsonify({'status': 'success', 'message': 'Swap request sent!'})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/notifications')
@login_required
def notifications_page():
    # Fetch notifications
    notifs = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).all()
    
    # Fetch pending swap requests where I am the target
    pending_swaps = ShiftSwapRequest.query.filter_by(target_user_id=current_user.id, status='pending_peer').all()
    
    return render_template('notifications.html', notifications=notifs, pending_swaps=pending_swaps)

@app.route('/api/respond_swap', methods=['POST'])
@login_required
def respond_swap():
    data = request.json
    request_id = data.get('request_id')
    action = data.get('action') # 'accept' or 'reject'
    
    if not request_id or not action:
         return jsonify({'status': 'error', 'message': 'Missing data'}), 400
         
    try:
        req = db.session.get(ShiftSwapRequest, request_id)
        if not req:
            return jsonify({'status': 'error', 'message': 'Request not found'}), 404
            
        if req.target_user_id != current_user.id:
            return jsonify({'status': 'error', 'message': 'Unauthorized'}), 403
            
        if action == 'reject':
            req.status = 'rejected'
            # Notify requester
            db.session.add(Notification(
                user_id=req.requester_id,
                message=f"Your swap request for {req.target_shift.date} was rejected.",
                link='/notifications'
            ))
            db.session.commit()
            return jsonify({'status': 'success', 'message': 'Request rejected'})
            
        elif action == 'accept':
            req.status = 'pending_admin'
            # Notify Admin(s)
            admins = User.query.filter_by(role='manager').all()
            for admin in admins:
                db.session.add(Notification(
                    user_id=admin.id,
                    message=f"Swap Request Pending Confirmation: {req.requester.username} <-> {current_user.username}",
                    link=url_for('admin_swaps_page')
                ))
            db.session.commit()
            return jsonify({'status': 'success', 'message': 'Request accepted. Waiting for Admin confirmation.'})
            
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/admin/swaps')
@login_required
@role_required('manager')
def admin_swaps_page():
    pending_swaps = ShiftSwapRequest.query.filter_by(status='pending_admin').all()
    return render_template('admin_swaps.html', pending_swaps=pending_swaps)

@app.route('/api/admin_confirm_swap', methods=['POST'])
@login_required
@role_required('manager')
def admin_confirm_swap():
    data = request.json
    request_id = data.get('request_id')
    action = data.get('action') # 'approve' or 'reject'
    
    try:
        req = db.session.get(ShiftSwapRequest, request_id)
        if not req: return jsonify({'status': 'error', 'message': 'Not found'}), 404
        
        if action == 'reject':
            req.status = 'rejected_by_admin'
            # Notify both
            create_notif(req.requester_id, "Admin rejected your swap request.")
            create_notif(req.target_user_id, "Admin rejected the swap request.")
            db.session.commit()
            return jsonify({'status': 'success', 'message': 'Rejected'})
            
        elif action == 'approve':
            # EXECUTE SWAP
            s1 = req.requester_shift
            s2 = req.target_shift
            
            p1 = s1.pediatrician_id
            p2 = s2.pediatrician_id
            
            s1.pediatrician_id = p2
            s2.pediatrician_id = p1
            
            req.status = 'approved'
            
            create_notif(req.requester_id, "Swap Approved! Calendar updated.")
            create_notif(req.target_user_id, "Swap Approved! Calendar updated.")
            
            db.session.commit()
            return jsonify({'status': 'success', 'message': 'Swap Executed!'})
            
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500

def create_notif(user_id, msg):
    db.session.add(Notification(user_id=user_id, message=msg, link='/notifications'))
    

if __name__ == '__main__':
    # Initialize database before running the app
    init_db_and_seed()
    # The debug=True line should be removed for final deployment
    app.run(debug=True)
