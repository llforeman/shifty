from flask import Flask, render_template, request, redirect, url_for, abort, jsonify, g, flash
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import date, datetime, timedelta
import calendar
from validation import check_overlap, get_validation_alerts
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

# Initialize extensions
# db = SQLAlchemy(app) # Already initialized above
migrate = Migrate(app, db) # Initialize Flask-Migrate
login_manager = LoginManager(app)
login_manager.login_view = 'login'

@app.before_request
def load_service_context():
    if current_user.is_authenticated and current_user.active_service:
        g.current_service = current_user.active_service
    else:
        g.current_service = None

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
# --- Multi-tenancy Models ---
class Organization(db.Model):
    __tablename__ = 'organization'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    services = db.relationship('Service', backref='organization', lazy=True)

class Service(db.Model):
    __tablename__ = 'service'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    organization_id = db.Column(db.Integer, db.ForeignKey('organization.id'), nullable=False)
    
    # Relationships
    pediatricians = db.relationship('Pediatrician', backref='service', lazy=True)
    users = db.relationship('User', backref='active_service', foreign_keys='User.active_service_id', lazy=True)
    configs = db.relationship('GlobalConfig', backref='service', lazy=True)
    activity_types = db.relationship('ActivityType', backref='service', lazy=True)

class Pediatrician(db.Model):
    __tablename__ = 'pediatrician'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False) # Removed unique=True globally, should be unique per service
    service_id = db.Column(db.Integer, db.ForeignKey('service.id'), nullable=True) # Nullable for migration
    
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
    email = db.Column(db.String(120), unique=True, nullable=True)
    password_hash = db.Column(db.String(255))
    role = db.Column(db.String(50), default='user') # 'manager' or 'user'
    must_change_password = db.Column(db.Boolean, default=False)
    
    pediatrician_id = db.Column(db.Integer, db.ForeignKey('pediatrician.id'), nullable=True) # Null for managers
    active_service_id = db.Column(db.Integer, db.ForeignKey('service.id'), nullable=True) # The service they are currently viewing/managing
    
    # Relationship to access pediatrician data
    pediatrician = db.relationship('Pediatrician', backref='users', foreign_keys=[pediatrician_id], lazy=True)

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

class GlobalConfig(db.Model):
    __tablename__ = 'global_config'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), nullable=False) # Not unique globally anymore, unique per service
    value = db.Column(db.String(255), nullable=False)
    service_id = db.Column(db.Integer, db.ForeignKey('service.id'), nullable=True) # Nullable for migration

    # Composite Unique Constraint: Key must be unique per service
    __table_args__ = (db.UniqueConstraint('key', 'service_id', name='_config_service_uc'),)

    def __repr__(self):
        return f"<GlobalConfig {self.key}={self.value}>"

class ShiftSwapRequest(db.Model):
    __tablename__ = 'shift_swap_request'
    
    id = db.Column(db.Integer, primary_key=True)
    requester_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    requester_shift_id = db.Column(db.Integer, db.ForeignKey('shift.id'), nullable=False)
    target_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    target_shift_id = db.Column(db.Integer, db.ForeignKey('shift.id'), nullable=False)
    status = db.Column(db.String(20), default='pending_peer')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    requester = db.relationship('User', foreign_keys=[requester_id], backref='sent_swap_requests')
    target_user = db.relationship('User', foreign_keys=[target_user_id], backref='received_swap_requests')
    requester_shift = db.relationship('Shift', foreign_keys=[requester_shift_id])
    target_shift = db.relationship('Shift', foreign_keys=[target_shift_id])

    def __repr__(self):
        return f"<ShiftSwapRequest {self.id} Status: {self.status}>"

class Notification(db.Model):
    __tablename__ = 'notification'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    message = db.Column(db.String(255), nullable=False)
    link = db.Column(db.String(255))
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    user = db.relationship('User', backref='notifications')

    def __repr__(self):
        return f"<Notification {self.user_id}: {self.message}>"

class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False) # Sender
    recipient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True) # Receiver (True for now to support public chat if needed, but we aim for DM)
    message = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
    sender = db.relationship('User', foreign_keys=[user_id], backref='sent_messages')
    recipient = db.relationship('User', foreign_keys=[recipient_id], backref='received_messages')

    def to_dict(self):
        return {
            'id': self.id,
            'sender_id': self.user_id,
            'sender_name': self.sender.username,
            'recipient_id': self.recipient_id,
            'message': self.message,
            'timestamp': self.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            'is_me': (self.user_id == current_user.id)
        }

class ActivityType(db.Model):
    __tablename__ = 'activity_type'
    
    id = db.Column(db.Integer, primary_key=True)
    # name is unique PER SERVICE
    name = db.Column(db.String(100), nullable=False)
    service_id = db.Column(db.Integer, db.ForeignKey('service.id'), nullable=True) # Nullable for migration
    min_staff = db.Column(db.Integer, nullable=True)
    max_staff = db.Column(db.Integer, nullable=True)
    
    __table_args__ = (db.UniqueConstraint('name', 'service_id', name='uq_activity_name_service'),)
    
    def __repr__(self):
        return f"<ActivityType {self.name}>"

class Activity(db.Model):
    __tablename__ = 'activity'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    # name and place are deprecated but kept effectively nullable for migration safety if needed, 
    # though we will try to migrate data to ActivityType.
    name = db.Column(db.String(100), nullable=True) 
    place = db.Column(db.String(200), nullable=True)
    
    activity_type_id = db.Column(db.Integer, db.ForeignKey('activity_type.id'), nullable=True) # Check nullable for migration steps
    
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    recurrence_type = db.Column(db.String(20), default='once')  # 'once' or 'weekly'
    recurrence_day = db.Column(db.Integer)  # 0-6 for Mon-Sun, null if 'once'
    recurrence_end_date = db.Column(db.Date)  # When to stop recurring
    
    user = db.relationship('User', backref='activities')
    activity_type = db.relationship('ActivityType', backref='activities')
    
    def __repr__(self):
        type_name = self.activity_type.name if self.activity_type else (self.name or 'Unknown')
        return f"<Activity {type_name} for User {self.user_id}>"
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.activity_type.name if self.activity_type else self.name,
            'start_time': self.start_time.strftime('%Y-%m-%d %H:%M:%S'),
            'end_time': self.end_time.strftime('%Y-%m-%d %H:%M:%S'),
            'recurrence_type': self.recurrence_type,
            'recurrence_day': self.recurrence_day,
            'recurrence_end_date': self.recurrence_end_date.strftime('%Y-%m-%d') if self.recurrence_end_date else None,
            'activity_type_id': self.activity_type_id
        }

class ActivityException(db.Model):
    __tablename__ = 'activity_exception'
    
    id = db.Column(db.Integer, primary_key=True)
    activity_id = db.Column(db.Integer, db.ForeignKey('activity.id'), nullable=False)
    date = db.Column(db.Date, nullable=False) # The specific date to skip
    
    activity = db.relationship('Activity', backref='exceptions')
    
    __table_args__ = (db.UniqueConstraint('activity_id', 'date', name='_act_date_uc'),)

    def __repr__(self):
        return f"<ActivityException {self.activity_id} on {self.date}>"

@app.route('/chat')
@login_required
def chat_page():
    return render_template('chat.html')

@app.route('/api/users', methods=['GET'])
@login_required
def get_chat_users():
    # Return list of other users to chat with
    users = User.query.filter(User.id != current_user.id).all()
    return jsonify([{'id': u.id, 'username': u.username} for u in users])

@app.route('/api/messages/<int:partner_id>', methods=['GET'])
@login_required
def get_conversation(partner_id):
    # Get messages between current_user and partner_id
    # (my sent to them) OR (their sent to me)
    msgs = ChatMessage.query.filter(
        db.or_(
            db.and_(ChatMessage.user_id == current_user.id, ChatMessage.recipient_id == partner_id),
            db.and_(ChatMessage.user_id == partner_id, ChatMessage.recipient_id == current_user.id)
        )
    ).order_by(ChatMessage.timestamp.asc()).all() # Oldest first for chat log
    
    return jsonify([m.to_dict() for m in msgs])

@app.route('/api/messages', methods=['POST'])
@login_required
def post_message():
    data = request.json
    recipient_id = data.get('recipient_id')
    content = data.get('message')
    
    if not content or not recipient_id:
        return jsonify({'status': 'error', 'message': 'Missing content or recipient'}), 400
        
    msg = ChatMessage(user_id=current_user.id, recipient_id=recipient_id, message=content)
    db.session.add(msg)
    db.session.commit()
    
    return jsonify({'status': 'success', 'message': 'Sent'})

@app.route('/activities')
@login_required
def activities_page():
    # Only show user's own activities + recurring logic will be handled in calendar
    activities = Activity.query.filter_by(user_id=current_user.id).order_by(Activity.start_time).all()
    activity_types = ActivityType.query.order_by(ActivityType.name).all()
    # Fetch activity types for the modal
    # Fetch activity types for the modal
    activity_types = ActivityType.query.filter_by(service_id=g.current_service.id).order_by(ActivityType.name).all()

    # View selection
    view_mode = request.args.get('view', 'week') # 'week' or 'month'
    
    # Dates
    today = date.today()
    start_str = request.args.get('start_date')
    
    if view_mode == 'month':
        # MONTHLY VIEW LOGIC
        year = int(request.args.get('year', today.year))
        month = int(request.args.get('month', today.month))
        
        # Navigation
        if month == 1:
            prev_month, prev_year = 12, year - 1
            prev_date = date(prev_year, prev_month, 1)
        else:
            prev_month, prev_year = month - 1, year
            prev_date = date(prev_year, prev_month, 1)
            
        if month == 12:
            next_month, next_year = 1, year + 1
            next_date = date(next_year, next_month, 1)
        else:
            next_month, next_year = month + 1, year
            next_date = date(next_year, next_month, 1)
            
        # Get calendar matrix
        cal = calendar.monthcalendar(year, month)
        
        # Date range for fetching
        start_date = date(year, month, 1)
        _, last_day = calendar.monthrange(year, month)
        end_date = date(year, month, last_day)
        
        # Fetch Activities (and expand recurring)
        raw_activities = Activity.query.filter_by(user_id=current_user.id).all()
        
        # Expand activities into a list of dicts for the template
        monthly_events = {}
        for day in range(1, last_day + 1):
            monthly_events[day] = []
            
        for act in raw_activities:
            # Check exceptions
            exceptions = {ex.date for ex in act.exceptions}
            
            # Dates this activity occurs in this month
            act_dates = []
            if act.recurrence_type == 'once':
                d = act.start_time.date()
                if start_date <= d <= end_date and d not in exceptions:
                    act_dates.append(d)
            elif act.recurrence_type == 'weekly':
                # Expand weekday to dates in this month
                # act.recurrence_day is 0-6 (Mon-Sun)
                # We can iterate through the month
                curr = start_date
                while curr <= end_date:
                    if curr.weekday() == act.recurrence_day:
                         if (not act.recurrence_end_date or curr <= act.recurrence_end_date) and curr >= act.start_time.date():
                            if curr not in exceptions:
                                act_dates.append(curr)
                    curr += timedelta(days=1)
            
            for d in act_dates:
                # Add to monthly_events
                s_iso = datetime.combine(d, act.start_time.time()).strftime('%Y-%m-%dT%H:%M')
                e_iso = datetime.combine(d, act.end_time.time()).strftime('%Y-%m-%dT%H:%M')
                
                monthly_events[d.day].append({
                    'id': act.id,
                    'title': act.activity_type.name if act.activity_type else (act.name or 'Unknown'),
                    'time': act.start_time.strftime('%H:%M'),
                    'start_iso': s_iso,
                    'end_iso': e_iso,
                    'activity_type_id': act.activity_type_id,
                    'recurrence_type': act.recurrence_type,
                    'color': '#4299e1'
                })
        
        # -----------------
        # ADD SHIFTS TO CALENDAR
        # -----------------
        if current_user.pediatrician_id:
            shifts = Shift.query.filter(
                Shift.pediatrician_id == current_user.pediatrician_id,
                Shift.date >= start_date,
                Shift.date <= end_date
            ).all()
            
            for shift in shifts:
                 monthly_events[shift.date.day].append({
                    'id': f"shift_{shift.id}", # distinct ID format
                    'title': f"{shift.type} (Shift)", # Indicate it's a shift
                    'time': '00:00', # Shifts usually imply ~24h or set blocks, handle as all-day or 00:00
                    'start_iso': shift.date.strftime('%Y-%m-%dT00:00'),
                    'end_iso': shift.date.strftime('%Y-%m-%dT23:59'),
                    'activity_type_id': '',
                    'recurrence_type': 'once',
                    'color': '#48bb78', # Green for assigned shifts
                    'is_shift': True # Flag to disable editing if needed
                })
        
        # Sort events by time
        for day in monthly_events:
            monthly_events[day].sort(key=lambda x: x['time'])
            
        month_name = date(year, month, 1).strftime('%B')
        
        return render_template('monthly_activities.html',
                               year=year, month=month, month_name=month_name,
                               month_calendar=cal, events=monthly_events,
                               prev_year=prev_year, prev_month=prev_month,
                               next_year=next_year, next_month=next_month,
                               activity_types=activity_types,
                               view_mode='month')

    else:
        # WEEKLY VIEW LOGIC
        if start_str:
            start_date = datetime.strptime(start_str, '%Y-%m-%d').date()
        else:
            # Start on Monday of current week
            start_date = today - timedelta(days=today.weekday())
            
        end_date = start_date + timedelta(days=6)
        
        # Navigation
        prev_week = (start_date - timedelta(days=7)).strftime('%Y-%m-%d')
        next_week = (start_date + timedelta(days=7)).strftime('%Y-%m-%d')
        
        # 1. Get Shifts
        shifts = Shift.query.filter(
            Shift.pediatrician_id == current_user.pediatrician_id,
            Shift.date >= start_date,
            Shift.date <= end_date
        ).all()
        
        # 2. Get Activities
        # Fetch all user activities (we filter recurrence manually)
        raw_activities = Activity.query.filter_by(user_id=current_user.id).all()
        
        from sqlalchemy import func, distinct
        
        # --- STAFFING VALIDATION (Max Staff) ---
        # Map limits
        type_limits = {at.id: at.max_staff for at in activity_types if at.max_staff}
        
        # Fetch Global Counts for this week (Unique Users per Type per Day)
        # Assuming database returns date object or string for func.date
        global_counts_query = db.session.query(
            Activity.activity_type_id, 
            func.date(Activity.start_time), 
            func.count(distinct(Activity.user_id))
        ).join(Activity.user).filter(
            User.active_service_id == g.current_service.id,
            Activity.start_time >= datetime.combine(start_date, datetime.min.time()),
            Activity.end_time <= datetime.combine(end_date, datetime.max.time())
        ).group_by(
            Activity.activity_type_id, 
            func.date(Activity.start_time)
        ).all()
        
        # Map: (type_id, date_obj_or_str) -> count
        global_counts = {(r[0], r[1]): r[2] for r in global_counts_query}

        # 3. Build Hourly Grid
        # Structure: events_by_day[0..6] = [ {title, start_hour, end_hour, type='shift'|'activity', place} ]
        events_by_day = {i: [] for i in range(7)}
        
        # Process Shifts
        for shift in shifts:
            day_idx = (shift.date - start_date).days
            if 0 <= day_idx <= 6:
                # Weekday: 17:00 - 24:00 (5pm-12am) = 7 hours
                # Weekend: 09:00 - 24:00 (9am-12am) = 15 hours
                is_weekend = (shift.date.weekday() >= 5) # 5=Sat, 6=Sun
                
                s_hour = 9 if is_weekend else 17
                e_hour = 24
                
                events_by_day[day_idx].append({
                    'title': 'Guardia',
                    'place': 'Hospital',
                    'start_hour': s_hour,
                    'end_hour': e_hour,
                    'type': 'shift',
                    'color': '#48bb78' # Green
                })
                
        # Process Activities
        for act in raw_activities:
            # Get exceptions for this activity
            exceptions = {ex.date for ex in act.exceptions}
            
            # Determine if activity occurs in this week
            act_dates = []
            
            if act.recurrence_type == 'once':
                d = act.start_time.date()
                if start_date <= d <= end_date and d not in exceptions:
                    act_dates.append(d)
            
            elif act.recurrence_type == 'weekly':
                # Find date of this weekday in current week
                diff = act.recurrence_day - start_date.weekday()
                target_date = start_date + timedelta(days=diff)
                
                # Check end date
                if (not act.recurrence_end_date or target_date <= act.recurrence_end_date) and target_date >= act.start_time.date():
                    if target_date not in exceptions and start_date <= target_date <= end_date:
                        act_dates.append(target_date)
                    
            for d in act_dates:
                day_idx = (d - start_date).days
                s_hour = act.start_time.hour
                e_hour = act.end_time.hour + (act.end_time.minute / 60.0)
                if e_hour == 0: e_hour = 24 # Handle midnight end
                
                # Helper to format time strings for the form
                start_iso = datetime.combine(d, act.start_time.time()).strftime('%Y-%m-%dT%H:%M')
                end_iso = datetime.combine(d, act.end_time.time()).strftime('%Y-%m-%dT%H:%M')
                if e_hour == 24: 
                     end_iso = (datetime.combine(d, datetime.min.time()) + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
    
                # Determine Color (Check Staffing Limit Violation)
                color = '#4299e1' # Blue Default
                if act.activity_type_id in type_limits:
                     limit = type_limits[act.activity_type_id]
                     # Check global count for this day
                     # Handle potential Date vs String key type match logic
                     c = global_counts.get((act.activity_type_id, d), 0)
                     if c == 0:
                          c = global_counts.get((act.activity_type_id, str(d)), 0)
                          
                     if c > limit:
                         color = '#e74c3c' # Red
                
                events_by_day[day_idx].append({
                    'id': act.id,
                    'title': act.activity_type.name if act.activity_type else (act.name or 'Unknown'),
                    'place': act.place,
                    'start_hour': s_hour,
                    'end_hour': e_hour,
                    'type': 'activity',
                    'color': color,
                    'activity_type_id': act.activity_type_id,
                    'start_iso': start_iso,
                    'end_iso': end_iso,
                    'recurrence_type': act.recurrence_type
                })
    
        # 4. Handle Overlaps (Calculate Width and Left)
        for day_idx in range(7):
            day_events = events_by_day[day_idx]
            if not day_events:
                continue
                
            # Sort by start time
            day_events.sort(key=lambda x: x['start_hour'])
            
            # Simple clustering algorithm
            # We group events that overlap.
            # Two events overlap if A.start < B.end and B.start < A.end
            
            clusters = []
            current_cluster = []
            cluster_end = -1
            
            for evt in day_events:
                if not current_cluster:
                    current_cluster.append(evt)
                    cluster_end = evt['end_hour']
                else:
                    # Check overlap with ANY event in cluster? Or just the cluster range?
                    # User request: "only in case that 2 activities exist at the same time"
                    # If we have [10-12, 10-12, 11-13], they all overlap effectively in a chain.
                    # A simple packing: if start < cluster_end, it belongs to cluster.
                    if evt['start_hour'] < cluster_end:
                        current_cluster.append(evt)
                        cluster_end = max(cluster_end, evt['end_hour'])
                    else:
                        # Mark conflicts
                        if len(current_cluster) > 1:
                             for e in current_cluster: e['color'] = '#e74c3c'
                        clusters.append(current_cluster)
                        current_cluster = [evt]
                        cluster_end = evt['end_hour']
            if current_cluster:
                if len(current_cluster) > 1:
                     for e in current_cluster: e['color'] = '#e74c3c'
                clusters.append(current_cluster)
                
            # Assign width and left
            for cluster in clusters:
                count = len(cluster)
                width = 100.0 / count
                for i, evt in enumerate(cluster):
                    evt['width'] = width
                    evt['left'] = i * width
    
        days = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']
        week_dates = [(start_date + timedelta(days=i)) for i in range(7)]
        
        return render_template('weekly_calendar.html', 
                               start_date=start_date,
                               week_dates=week_dates,
                               days=days,
                               events_by_day=events_by_day,
                               prev_week=prev_week,
                               next_week=next_week,
                               activity_types=activity_types,
                               view_mode='week')

@app.route('/activities/add', methods=['POST'])
@login_required
def add_activity():
    try:
        activity_id = request.form.get('activity_id')
        activity_type_id = request.form.get('activity_type_id')
        start_time_str = request.form.get('start_time')
        end_time_str = request.form.get('end_time')
        recurrence_type = request.form.get('recurrence_type', 'once')
        
        # Parse Dates
        start_time = datetime.fromisoformat(start_time_str)
        end_time = datetime.fromisoformat(end_time_str)
        
        if activity_id:
            # Update existing
            activity = Activity.query.get_or_404(activity_id)
            if activity.user_id != current_user.id:
                abort(403)
            
            # Validation: Overlap (Exclude self)
            if check_overlap(current_user.id, start_time, end_time, exclude_activity_id=activity.id):
                 flash("Warning: Conflict detected! You have another activity at this time.", "warning")

            from validation import check_max_staff_limit
            # Validation: Max Staff
            is_limit, limit, curr = check_max_staff_limit(activity_type_id, start_time.date(), current_user.id)
            if is_limit:
                 flash(f"Warning: Staff limit exceeded! Max {limit}.", "warning")

            activity.activity_type_id = activity_type_id
            activity.start_time = start_time
            activity.end_time = end_time
            activity.recurrence_type = recurrence_type
            # Reset recurring day if needed
            if recurrence_type == 'weekly':
                activity.recurrence_day = start_time.weekday()
            else:
                 activity.recurrence_day = None
                 
        else:
            # Create new
            recurrence_day = start_time.weekday() if recurrence_type == 'weekly' else None
            
            # Validation: Overlap
            if check_overlap(current_user.id, start_time, end_time):
                 flash("Warning: Conflict detected! You have another activity at this time.", "warning")

            from validation import check_max_staff_limit
            # Validation: Max Staff
            is_limit, limit, curr = check_max_staff_limit(activity_type_id, start_time.date(), current_user.id)
            if is_limit:
                 flash(f"Warning: Staff limit exceeded! Max {limit}.", "warning")

            activity = Activity(
                user_id=current_user.id,
                activity_type_id=activity_type_id,
                start_time=start_time,
                end_time=end_time,
                recurrence_type=recurrence_type,
                recurrence_day=recurrence_day
            )
            db.session.add(activity)
            
        db.session.commit()
    except Exception as e:
        print(f"Error saving activity: {e}")
        flash(f"Error saving activity: {e}", "error")
        
    return redirect(url_for('activities_page'))

@app.route('/activities/delete/<int:id>', methods=['POST'])
@login_required
def delete_activity(id):
    activity = Activity.query.get_or_404(id)
    if activity.user_id != current_user.id:
        abort(403)
    
    # New logic for handling recurrence deletion
    delete_mode = request.form.get('delete_mode', 'all')
    target_date_str = request.form.get('date') # Expected from UI for specific instance
    
    try:
        if delete_mode == 'single' and activity.recurrence_type == 'weekly' and target_date_str:
            # Add exception for this date
            try:
                target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
                if not ActivityException.query.filter_by(activity_id=activity.id, date=target_date).first():
                    exception = ActivityException(activity_id=activity.id, date=target_date)
                    db.session.add(exception)
                    db.session.commit()
                    print(f"Added exception for activity {activity.id} on {target_date}")
            except ValueError:
                print("Invalid date format for deletion exception")
        else:
            # Default: delete the whole activity
            # Use cascade delete? Or manually delete exceptions first?
            ActivityException.query.filter_by(activity_id=id).delete()
            db.session.delete(activity)
            db.session.commit()
            print(f"Deleted activity {activity.id}")
            
    except Exception as e:
        print(f"Error deleting activity: {e}")
        db.session.rollback()
        
    return redirect(url_for('activities_page'))

# -----------------
# 2. DATABASE INITIALIZATION (Run this once to create tables)
# -----------------
def seed_global_config(service_id):
    """Seeds the database with default configuration values and handles schema updates."""
    # ... schema migration check skipped or assumed done elsewhere ...
    
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
        if not GlobalConfig.query.filter_by(key=key, service_id=service_id).first():
            db.session.add(GlobalConfig(key=key, value=value, service_id=service_id))
    db.session.commit()
    print(f"Seeded GlobalConfig with default values for service {service_id}.")

def init_db_and_seed():
    """Creates tables and adds initial test data if none exists."""
    with app.app_context():
        # Creates all tables defined by the Models
        db.create_all()
        print("Database tables created.")
        
        # Create Default Organization and Service if not exist
        default_org = Organization.query.first()
        if not default_org:
            default_org = Organization(name='Hospital General')
            db.session.add(default_org)
            db.session.commit()
            print("Seeded default Organization.")
    
        default_service = Service.query.filter_by(organization_id=default_org.id).first()
        if not default_service:
            default_service = Service(name='Pediatría', organization_id=default_org.id)
            db.session.add(default_service)
            db.session.commit()
            print("Seeded default Service.")
        
        # Seed global config
        seed_global_config(default_service.id)
        
        # Add a test pediatrician if the table is empty for this service
        if Pediatrician.query.filter_by(service_id=default_service.id).count() == 0:
            test_ped = Pediatrician(name="Dr. Test User", min_shifts=3, max_shifts=6, min_weekend=1, max_weekend=2, service_id=default_service.id)
            db.session.add(test_ped)
            db.session.commit() # Get ID
    
            print(f"Seeded database with one test user (Dr. Test User, ID={test_ped.id}).")
    
            # Create a login user for this pediatrician
            if not User.query.filter_by(username='dr_test').first():
                user = User(username='dr_test', role='user', pediatrician_id=test_ped.id, active_service_id=default_service.id)
                user.set_password('password')
                db.session.add(user)
                db.session.commit()
                print("Created test user (dr_test/password) linked to Dr. Test User")
    
        # Create default admin user if not exists
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', role='manager', active_service_id=default_service.id)
            admin.set_password('admin123') # Change this in production!
            db.session.add(admin)
            db.session.commit()
            print("Created default admin user (admin/admin123)")

        # Create Superadmin user if not exists
        if not User.query.filter_by(username='superadmin').first():
            superadmin = User(username='superadmin', role='superadmin') # No service_id
            superadmin.set_password('superadmin123')
            db.session.add(superadmin)
            db.session.commit()
            print("Created superadmin user (superadmin/superadmin123)")

# Initialize database when app starts (within proper context)
with app.app_context():
    try:
        db.create_all()
        print("Database tables created.")
        
        # AUTO-MIGRATION: Add recipient_id to chat_message if it doesn't exist
        try:
            from sqlalchemy import inspect
            inspector = inspect(db.engine)
            columns = [c['name'] for c in inspector.get_columns('chat_message')]
            
            if 'recipient_id' not in columns:
                print("[MIGRATION] Adding recipient_id column to chat_message...")
                with db.engine.connect() as conn:
                    conn.execute(db.text("ALTER TABLE chat_message ADD COLUMN recipient_id INTEGER"))
                    conn.commit()
                print("[MIGRATION] Successfully added recipient_id column!")
            else:
                print("[MIGRATION] recipient_id column already exists, skipping migration.")
        except Exception as e:
            print(f"[MIGRATION] Migration check failed (safe to ignore if table doesn't exist yet): {e}")
            
    except Exception as e:
        print(f"Database initialization skipped: {e}")

    # AUTO-MIGRATION: Activity Refactor
    try:
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        columns = [c['name'] for c in inspector.get_columns('activity')]
        
        # 1. Add activity_type_id column if missing
        if 'activity_type_id' not in columns:
            print("[MIGRATION] Adding activity_type_id to activity table...")
            with db.engine.connect() as conn:
                conn.execute(db.text("ALTER TABLE activity ADD COLUMN activity_type_id INTEGER"))
                conn.commit()
        
        # 2. Populate ActivityTypes from existing names and link them
        # We need to do this carefully. 
        # Since we are inside app context but maybe not in a clean request, we use db.session carefully.
        
        # Ensure ActivityType table exists (create_all above should have created it if not exists)
        
        # Check if there are activities with name but no type
        # We use raw sql or simple queries.
        
        # Get all distinct names from activities where activity_type_id is NULL
        # Note: If 'name' column exists and has data.
        if 'name' in columns:
            stmt = db.text("SELECT DISTINCT name FROM activity WHERE activity_type_id IS NULL AND name IS NOT NULL")
            with db.engine.connect() as conn:
                result = conn.execute(stmt)
                distinct_names = [row[0] for row in result]
            
            if distinct_names:
                print(f"[MIGRATION] Found {len(distinct_names)} distinct activity names to migrate to ActivityType.")
                for act_name in distinct_names:
                    # Create or get ActivityType
                    act_type = ActivityType.query.filter_by(name=act_name).first()
                    if not act_type:
                        act_type = ActivityType(name=act_name)
                        db.session.add(act_type)
                        db.session.commit() # Commit to get ID
                    
                    # Update all activities with this name
                    db.session.execute(
                        db.text("UPDATE activity SET activity_type_id = :type_id WHERE name = :name AND activity_type_id IS NULL"),
                        {'type_id': act_type.id, 'name': act_name}
                    )
                db.session.commit()
                print("[MIGRATION] Activity data migration completed.")
                
    except Exception as e:
        print(f"[MIGRATION] Activity migration failed: {e}")



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
        
        # Handle bulk calendar updates
        elif preference_mode == 'calendar':
            import json
            calendar_changes_json = request.form.get('calendar_changes')
            if calendar_changes_json:
                try:
                    changes = json.loads(calendar_changes_json)
                    for date_str, type_val in changes.items():
                        change_date = date.fromisoformat(date_str)
                        
                        existing_entry = Preference.query.filter_by(
                            pediatrician_id=ped_id, date=change_date
                        ).first()
                        
                        if type_val: # Update or Create
                            if existing_entry:
                                existing_entry.type = type_val
                                existing_entry.recurring_group = None
                            else:
                                new_pref = Preference(
                                    pediatrician_id=ped_id, 
                                    date=change_date, 
                                    type=type_val
                                )
                                db.session.add(new_pref)
                        else: # Delete (type_val is null)
                            if existing_entry:
                                db.session.delete(existing_entry)
                    
                    db.session.commit()
                    flash('Cambios del calendario guardados correctamente.', 'success')
                    return redirect(url_for('preferences_page', ped_id=ped_id))
                    
                except Exception as e:
                    db.session.rollback()
                    print(f"Error processing calendar changes: {e}")
                    flash('Error al guardar cambios del calendario.', 'error')

        # Handle specific date preference
        elif preference_mode == 'specific':
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

        # Handle date range preference
        elif preference_mode == 'range':
            range_start_str = request.form.get('range_start')
            range_end_str = request.form.get('range_end')
            
            if range_start_str and range_end_str and req_type:
                try:
                    start_date = date.fromisoformat(range_start_str)
                    end_date = date.fromisoformat(range_end_str)
                    
                    # Iterate through range
                    current_date = start_date
                    while current_date <= end_date:
                        existing_entry = Preference.query.filter_by(
                            pediatrician_id=ped_id, date=current_date
                        ).first()

                        if req_type == 'Delete':
                            if existing_entry:
                                db.session.delete(existing_entry)
                        elif existing_entry:
                            existing_entry.type = req_type
                            existing_entry.recurring_group = None # Convert to individual if it was part of a group? 
                            # Decision: Yes, because specific overrides/edits should probably break the group link 
                            # or just stay individual. Let's make it individual.
                        else:
                            new_pref = Preference(
                                pediatrician_id=ped_id, 
                                date=current_date, 
                                type=req_type,
                                recurring_group=None
                            )
                            db.session.add(new_pref)
                        
                        current_date += timedelta(days=1)
                    
                    db.session.commit()
                    return redirect(url_for('preferences_page', ped_id=ped_id))
                except ValueError as e:
                    print(f"Error processing date range: {e}")
        
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
        
    # Prepare JSON for Calendar
    # List of { date: 'YYYY-MM-DD', type: 'Skip'|'Vacation'... }
    prefs_list = []
    for p in all_prefs:
        prefs_list.append({
            'date': p.date.strftime('%Y-%m-%d'),
            'type': p.type
        })
        
    return render_template(
        'preferences_form.html',
        pediatrician=pediatrician,
        individual_prefs=individual_prefs,
        recurring_prefs=formatted_recurring,
        prefs_json=prefs_list
    )

@app.route('/login', methods=['GET', 'POST'])
def login():
    from flask import session as flask_session
    print(f"[DEBUG] /login - Before login, session: {dict(flask_session)}")
    print(f"[DEBUG] /login - is_authenticated: {current_user.is_authenticated}")
    
    if current_user.is_authenticated:
        if current_user.role == 'superadmin':
            return redirect(url_for('superadmin_dashboard'))
        # Fixed: Send all users to profile, not just managers
        return redirect(url_for('profile'))
        
    if request.method == 'POST':
        username_or_email = request.form.get('username')
        password = request.form.get('password')
        
        # Try finding by username first, then by email
        user = User.query.filter_by(username=username_or_email).first()
        if not user:
            user = User.query.filter_by(email=username_or_email).first()

        if user and user.check_password(password):
            print(f"[DEBUG] /login - Login successful for user: {user.username}")
            login_user(user, remember=True)
            
            # Check for forced password change
            if user.must_change_password:
                flash('Por seguridad, debes cambiar tu contraseña inicial.', 'error')
                return redirect(url_for('profile'))
                
            if user.role == 'superadmin':
                 return redirect(url_for('superadmin_dashboard'))

            next_page = request.args.get('next')
            if next_page:
                return redirect(next_page)
            
            return redirect(url_for('profile'))
        
        print(f"[DEBUG] /login - Login failed for: {username_or_email}")
        return render_template('login.html', error='Usuario/Email o contraseña inválidos')

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
        update_mode = request.form.get('update_mode')
        
        if update_mode == 'password':
            new_password = request.form.get('new_password')
            confirm_password = request.form.get('confirm_password')
            
            if new_password and new_password == confirm_password:
                current_user.set_password(new_password)
                current_user.must_change_password = False # Clear forced flag
                db.session.commit()
                msg = 'Contraseña actualizada correctamente.'
                msg_category = 'success'
            else:
                msg = 'Las contraseñas no coinciden.'
                msg_category = 'error'
        
        elif update_mode == 'details':
            new_email = request.form.get('email')
            new_name = request.form.get('name')
            
            try:
                # Update User
                if new_email and new_email != current_user.email:
                    # Check uniqueness
                    if User.query.filter_by(email=new_email).first():
                         msg = 'El email ya está en uso.'
                         msg_category = 'error'
                    else:
                        current_user.email = new_email
                        current_user.username = new_email # Sync username
                        
                        # Update Pediatrician name if linked
                        if current_user.pediatrician and new_name:
                             # Check if name is taken by another ped?
                             other_ped = Pediatrician.query.filter(Pediatrician.name == new_name, Pediatrician.id != current_user.pediatrician_id).first()
                             if other_ped:
                                 msg = 'El nombre de pediatra ya existe.'
                                 msg_category = 'error'
                             else:
                                 current_user.pediatrician.name = new_name
                                 db.session.commit()
                                 msg = 'Información actualizada correctamente.'
                                 msg_category = 'success'
                        else:
                             db.session.commit()
                             msg = 'Email actualizado correctamente.'
                             msg_category = 'success'
                
                # Just name update
                elif current_user.pediatrician and new_name and new_name != current_user.pediatrician.name:
                     other_ped = Pediatrician.query.filter(Pediatrician.name == new_name, Pediatrician.id != current_user.pediatrician_id).first()
                     if other_ped:
                         msg = 'El nombre de pediatra ya existe.'
                         msg_category = 'error'
                     else:
                         current_user.pediatrician.name = new_name
                         db.session.commit()
                         msg = 'Nombre actualizado correctamente.'
                         msg_category = 'success'
                
            except Exception as e:
                db.session.rollback()
                print(f"Error updating profile: {e}")
                msg = f'Error al actualizar: {e}'
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
    logout_user()
    return redirect(url_for('login'))

@app.route('/prefs/selection')
@login_required
@role_required('manager')
def preferences_view_selection():
    pediatricians = Pediatrician.query.filter_by(service_id=g.current_service.id).order_by(Pediatrician.name).all()
    return render_template('preferences_selection.html', pediatricians=pediatricians)

@app.route('/manager_config', methods=['GET', 'POST'])
@login_required
@role_required('manager')
def manager_config():
    
    if request.method == 'POST':
        # Update values from form
        for key, value in request.form.items():
            # Skip the submit button or other non-config fields if any
            if key != 'submit':
                config_item = GlobalConfig.query.filter_by(key=key, service_id=g.current_service.id).first()
                if config_item:
                    config_item.value = value
                else:
                    # Create if it doesn't exist for this service (e.g. new service)
                    new_config = GlobalConfig(key=key, value=value, service_id=g.current_service.id)
                    db.session.add(new_config)
                    
        db.session.commit()
        return redirect(url_for('manager_config'))
    
    # Fetch all configs for current service
    config_items = GlobalConfig.query.filter_by(service_id=g.current_service.id).all()
    # Convert to dictionary for easier access in template
    config_dict = {item.key: item.value for item in config_items}
    
    # Get Validation Alerts (Today/Tomorrow)
    alerts = get_validation_alerts(g.current_service.id)
    
    return render_template('manager_config.html', config=config_dict, alerts=alerts)
    
@app.route('/admin/create_user', methods=['GET', 'POST'])
@login_required
@role_required('manager')
def admin_create_user():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        role = request.form.get('role')
        staff_type = request.form.get('staff_type')
        is_mir = request.form.get('is_mir') == 'yes'
        
        # Validation
        if User.query.filter_by(email=email).first():
            flash('El email ya está registrado.', 'error')
            return redirect(url_for('admin_create_user'))
            
        try:
            # 1. Handle Pediatrician (if role is user)
            ped_id = None
            if role == 'user':
                existing_ped = Pediatrician.query.filter_by(name=name, service_id=g.current_service.id).first()
                if existing_ped:
                    ped_id = existing_ped.id
                    # Update existing details just in case
                    existing_ped.type = staff_type
                    existing_ped.mir = is_mir
                else:
                    # Create new Pediatrician
                    new_ped = Pediatrician(
                        name=name, 
                        service_id=g.current_service.id,
                        type=staff_type,
                        mir=is_mir
                    )
                    db.session.add(new_ped)
                    db.session.commit()
                    ped_id = new_ped.id
            
            # 2. Create User
            # We use email as username to ensure uniqueness and simplicity
            new_user = User(
                username=email, 
                email=email, 
                role=role,
                pediatrician_id=ped_id,
                must_change_password=True,
                active_service_id=g.current_service.id
            )
            new_user.set_password('1111')
            
            db.session.add(new_user)
            db.session.commit()
            
            flash(f'Usuario {name} ({email}) creado con éxito.', 'success')
            return redirect(url_for('manager_config')) # Or back to list
            
        except Exception as e:
            db.session.rollback()
            print(f"Error creating user: {e}")
            flash(f'Error al crear usuario: {e}', 'error')
            
    # Fetch all users for display
    all_users = User.query.filter_by(active_service_id=g.current_service.id).options(db.joinedload(User.pediatrician)).order_by(User.id.desc()).all()
            
    return render_template('admin_create_user.html', users=all_users)


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
            start_year, start_month, end_year, end_month, g.current_service.id,
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
    """Debug route to see all shifts in database for current service"""
    all_shifts = Shift.query.join(Pediatrician).filter(Pediatrician.service_id == g.current_service.id).order_by(Shift.date).all()
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

        # 1. Clear existing live shifts for this range for THIS service
        # We find shifts belonging to peds in this service
        Shift.query.filter(
            Shift.date >= start_date, 
            Shift.date <= end_date,
            Shift.pediatrician.has(service_id=g.current_service.id)
        ).delete(synchronize_session=False)
        
        # 2. Get draft shifts for THIS service
        drafts = DraftShift.query.join(Pediatrician).filter(
            DraftShift.date >= start_date, 
            DraftShift.date <= end_date,
            Pediatrician.service_id == g.current_service.id
        ).all()
        
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
    shifts_query = ModelClass.query.join(Pediatrician).filter(
        ModelClass.date >= start_date, 
        ModelClass.date <= end_date,
        Pediatrician.service_id == g.current_service.id
    )
    
    # REMOVED: Regular users only see their own shifts logic. 
    # Now all users see all shifts to enable swapping.
    
    shifts_list = shifts_query.all()
    
    # If no shifts found for this month, check for future shifts (navigation help)
    next_shift_date = None
    if not shifts_list:
        next_shift_query = ModelClass.query.join(Pediatrician).filter(
            ModelClass.date > end_date,
            Pediatrician.service_id == g.current_service.id
        ).order_by(ModelClass.date).first()
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
                           month_calendar=cal, shifts=shifts_by_day,
                           prev_year=prev_year, prev_month=prev_month,
                           next_year=next_year, next_month=next_month,
                           next_shift_date=next_shift_date,
                           is_draft=is_draft,
                           current_user=current_user)

@app.route('/superadmin')
@login_required
@role_required('superadmin')
def superadmin_dashboard():
    organizations = Organization.query.all()
    return render_template('superadmin_dashboard.html', organizations=organizations)

@app.route('/superadmin/create_org', methods=['POST'])
@login_required
@role_required('superadmin')
def superadmin_create_org():
    name = request.form.get('name')
    if name:
        try:
            org = Organization(name=name)
            db.session.add(org)
            db.session.commit()
            flash('Hospital creado correctamente.', 'success')
        except Exception as e:
            flash(f'Error al crear hospital: {e}', 'error')
    return redirect(url_for('superadmin_dashboard'))

@app.route('/superadmin/create_service', methods=['POST'])
@login_required
@role_required('superadmin')
def superadmin_create_service():
    org_id = request.form.get('org_id')
    name = request.form.get('name')
    
    if org_id and name:
        try:
            service = Service(name=name, organization_id=org_id)
            db.session.add(service)
            db.session.commit()
            
            # Seed configs for new service
            seed_global_config(service.id)
            
            flash('Servicio creado correctamente.', 'success')
        except Exception as e:
            flash(f'Error al crear servicio: {e}', 'error')
    return redirect(url_for('superadmin_dashboard'))

@app.route('/superadmin/create_admin', methods=['POST'])
@login_required
@role_required('superadmin')
def superadmin_create_admin():
    service_id = request.form.get('service_id')
    username = request.form.get('username')
    password = request.form.get('password')
    
    if service_id and username and password:
        try:
            if User.query.filter_by(username=username).first():
                 flash('El usuario ya existe.', 'error')
            else:
                user = User(username=username, role='manager', active_service_id=service_id)
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                flash('Admin creado correctamente.', 'success')
        except Exception as e:
            flash(f'Error al crear admin: {e}', 'error')
    return redirect(url_for('superadmin_dashboard'))

    return redirect(url_for('superadmin_dashboard'))

@app.route('/superadmin/edit_org/<int:org_id>', methods=['POST'])
@login_required
@role_required('superadmin')
def superadmin_edit_org(org_id):
    name = request.form.get('name')
    if name:
        org = Organization.query.get_or_404(org_id)
        org.name = name
        db.session.commit()
        flash('Hospital renombrado correctamente.', 'success')
    return redirect(url_for('superadmin_dashboard'))

@app.route('/superadmin/edit_service/<int:service_id>', methods=['POST'])
@login_required
@role_required('superadmin')
def superadmin_edit_service(service_id):
    name = request.form.get('name')
    if name:
        service = Service.query.get_or_404(service_id)
        service.name = name
        db.session.commit()
        flash('Servicio renombrado correctamente.', 'success')
    return redirect(url_for('superadmin_dashboard'))

@app.route('/api/swap_shifts', methods=['POST'])
@login_required
@role_required('manager')
def swap_shifts():
    data = request.json
    source_id = data.get('source_id')
    target_id = data.get('target_id') # Optional: ID of shift being swapped with
    target_date_str = data.get('target_date') # Required: Date to move/swap to
    mode = data.get('mode') # 'draft' or 'live'
    
    if not source_id or not target_date_str:
        return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400
        
    try:
        ModelClass = DraftShift if mode == 'draft' else Shift
        target_date = date.fromisoformat(target_date_str)
        
        # Get source shift
        source_shift = db.session.get(ModelClass, source_id)
        if not source_shift:
            return jsonify({'status': 'error', 'message': 'Source shift not found'}), 404
            
        # CASE 1: Swapping with an existing shift
        if target_id:
            target_shift = db.session.get(ModelClass, target_id)
            if not target_shift:
                return jsonify({'status': 'error', 'message': 'Target shift not found'}), 404
            
            # Check for conflicts
            conflict_b = ModelClass.query.filter_by(pediatrician_id=target_shift.pediatrician_id, date=source_shift.date).first()
            if conflict_b and conflict_b.id != source_shift.id:
                 return jsonify({'status': 'error', 'message': f'Conflict: {target_shift.pediatrician.name} already has a shift on {source_shift.date}'}), 400

            conflict_a = ModelClass.query.filter_by(pediatrician_id=source_shift.pediatrician_id, date=target_shift.date).first()
            if conflict_a and conflict_a.id != target_shift.id:
                 return jsonify({'status': 'error', 'message': f'Conflict: {source_shift.pediatrician.name} already has a shift on {target_shift.date}'}), 400

            # Perform Swap
            p1 = source_shift.pediatrician_id
            p2 = target_shift.pediatrician_id
            
            source_shift.pediatrician_id = p2
            target_shift.pediatrician_id = p1
            
            db.session.commit()
            return jsonify({'status': 'success', 'message': 'Shifts swapped successfully'})

        # CASE 2: Moving to an empty slot
        else:
            # Check if source pediatrician already has a shift on target date
            existing = ModelClass.query.filter_by(pediatrician_id=source_shift.pediatrician_id, date=target_date).first()
            if existing:
                 return jsonify({'status': 'error', 'message': f'Conflict: {source_shift.pediatrician.name} already has a shift on {target_date}'}), 400
            
            # Move shift
            source_shift.date = target_date
            db.session.commit()
            return jsonify({'status': 'success', 'message': 'Shift moved successfully'})

    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/request_swap', methods=['POST'])
@login_required
def request_swap():
    data = request.json
    source_shift_id = data.get('source_shift_id')
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
    

@app.route('/admin/activity_types')
@login_required
@role_required('manager')
def admin_activity_types_page():
    # Filter by current service
    activity_types = ActivityType.query.filter_by(service_id=g.current_service.id).order_by(ActivityType.name).all()
    return render_template('admin_activity_types.html', activity_types=activity_types)

@app.route('/admin/activity_types/add', methods=['POST'])
@login_required
@role_required('manager')
def admin_add_activity_type():
    name = request.form.get('name')
    min_staff = request.form.get('min_staff', type=int)
    max_staff = request.form.get('max_staff', type=int)
    
    if name:
        # Check uniqueness in THIS service
        if not ActivityType.query.filter_by(name=name, service_id=g.current_service.id).first():
            new_type = ActivityType(
                name=name, 
                service_id=g.current_service.id,
                min_staff=min_staff,
                max_staff=max_staff
            )
            db.session.add(new_type)
            db.session.commit()
    return redirect(url_for('admin_activity_types_page'))

@app.route('/admin/activity_types/update/<int:id>', methods=['POST'])
@login_required
@role_required('manager')
def admin_update_activity_type(id):
    act_type = ActivityType.query.get_or_404(id)
    # Security check: belong to service
    if act_type.service_id != g.current_service.id:
        flash("Unauthorized access to this activity type.", "error")
        return redirect(url_for('admin_activity_types_page'))
        
    act_type.name = request.form.get('name')
    act_type.min_staff = request.form.get('min_staff', type=int)
    act_type.max_staff = request.form.get('max_staff', type=int)
    
    db.session.commit()
    flash("Activity Type updated.", "success")
    return redirect(url_for('admin_activity_types_page'))

@app.route('/api/debug/add_min_max_columns')
@login_required
@role_required('superadmin')
def debug_add_min_max_columns():
    from sqlalchemy import text
    try:
        conn = db.engine.connect()
        trans = conn.begin()
        
        # Add min_staff
        try:
            conn.execute(text("ALTER TABLE activity_type ADD COLUMN min_staff INTEGER"))
        except Exception as e:
            if "duplicate" not in str(e).lower():
                pass # Ignore if exists

        # Add max_staff
        try:
            conn.execute(text("ALTER TABLE activity_type ADD COLUMN max_staff INTEGER"))
        except Exception as e:
            if "duplicate" not in str(e).lower():
                pass

        trans.commit()
        conn.close()
        return "Migration successful: min_staff and max_staff columns added."
        
    except Exception as e:
        return f"Migration failed: {e}"

@app.route('/admin/activity_types/delete/<int:id>', methods=['POST'])
@login_required
@role_required('manager')
def admin_delete_activity_type(id):
    act_type = ActivityType.query.get_or_404(id)
    try:
        # Set activities to null type
        Activity.query.filter_by(activity_type_id=id).update({'activity_type_id': None})
        db.session.delete(act_type)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Error deleting activity type: {e}")
        
    return redirect(url_for('admin_activity_types_page'))
    

@app.route('/api/debug/create_superadmin')
def debug_create_superadmin():
    if not User.query.filter_by(username='superadmin').first():
        superadmin = User(username='superadmin', role='superadmin')
        superadmin.set_password('superadmin123')
        db.session.add(superadmin)
        db.session.commit()
        return "Superadmin created: superadmin / superadmin123"
        return "Superadmin exists. Password reset to: superadmin123"

@app.route('/api/debug/migrate_activity_types')
@login_required
@role_required('superadmin')
def debug_migrate_activity_types():
    from sqlalchemy import text
    try:
        # Check if column service_id exists in activity_type? 
        # Actually we know what we need: recreate table to fix constraints.
        
        conn = db.engine.connect()
        trans = conn.begin()
        
        # 1. Rename existing table
        conn.execute(text("ALTER TABLE activity_type RENAME TO activity_type_old"))
        
        # 2. Create new table with updated constraints:
        # - id PK
        # - name VARCHAR(100) NOT NULL
        # - service_id INTEGER, FK to service.id
        # - UNIQUE(name, service_id)
        # Note: In SQLite id is INTEGER PRIMARY KEY AUTOINCREMENT equivalent
        create_sql = """
        CREATE TABLE activity_type (
            id INTEGER PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            service_id INTEGER,
            FOREIGN KEY(service_id) REFERENCES service(id),
            CONSTRAINT uq_activity_name_service UNIQUE(name, service_id)
        );
        """
        conn.execute(text(create_sql))
        
        # 3. Copy data
        # Default service_id to 1 if null
        conn.execute(text("""
            INSERT INTO activity_type (id, name, service_id)
            SELECT id, name, COALESCE(service_id, 1) FROM activity_type_old
        """))
        
        # 4. Drop old table
        conn.execute(text("DROP TABLE activity_type_old"))
        
        trans.commit()
        conn.close()
        return "Migration successful: ActivityType unique constraint updated."
        
    except Exception as e:
        if 'already exists' in str(e):
             return f"Migration might have already run? Error: {e}"
        return f"Migration failed: {e}"

@app.route('/global_calendar')
@login_required
def global_calendar():
    # 1. Date Logic
    week_offset = request.args.get('week_offset', 0, type=int)
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
    end_of_week = start_of_week + timedelta(days=6)
    
    days = [start_of_week + timedelta(days=i) for i in range(7)]
    day_names = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']
    
    # 2. Fetch Data (Activities + Shifts)
    # Using specific service scope
    # Note: Shift doesn't store explicit start/end time usually, standard is morning? 
    # Or Shift is just "Guardia" which is 24h or specific?
    # For now assuming Shift = "Guardia" (24h or 8am-8am) and Activity has times.
    
    # Needs: Service filter
    if not g.current_service:
        flash("No service context.", "error")
        return redirect(url_for('profile'))

    activities = Activity.query.filter(
        Activity.user.has(active_service_id=g.current_service.id), # Filter by user's service? No, activity's context.
        # Actually Activity doesn't have service_id directly, but User does. 
        # But wait, User.active_service_id changes on login.
        # We need to filter generally by users belonging to this service (via Pediatrician).
        Activity.start_time >= datetime.combine(start_of_week, datetime.min.time()),
        Activity.end_time <= datetime.combine(end_of_week, datetime.max.time())
    ).all()
    
    # Calculate Staffing Violations (Max Staff)
    # 1. Map limits
    type_limits = {at.id: at.max_staff for at in ActivityType.query.filter_by(service_id=g.current_service.id).all() if at.max_staff is not None}
    
    # 2. Count Daily Staffing
    daily_counts = {} # (date, type_id) -> Set of user_ids
    for a in activities:
        if a.activity_type_id and a.activity_type_id in type_limits:
            d = a.start_time.date()
            k = (d, a.activity_type_id)
            if k not in daily_counts: daily_counts[k] = set()
            daily_counts[k].add(a.user_id)
            
    # 3. Identify Violation Keys
    violation_keys = set()
    for k, users in daily_counts.items():
        if len(users) > type_limits[k[1]]:
            violation_keys.add(k)

    # Calculate Overlaps for Visualization
    conflict_ids = set()
    # Sort by user then start_time to compare adjacent activities
    activities.sort(key=lambda x: (x.user_id, x.start_time))
    for i in range(len(activities)):
        for j in range(i + 1, len(activities)):
            a1 = activities[i]
            a2 = activities[j]
            if a1.user_id != a2.user_id:
                break
            # Check overlap: a1 ends after a2 starts
            if a1.end_time > a2.start_time: 
                conflict_ids.add(a1.id)
                conflict_ids.add(a2.id)
            else:
                break 

    # Shifts
    shifts = Shift.query.join(Pediatrician).filter(
        Pediatrician.service_id == g.current_service.id,
        Shift.date >= start_of_week,
        Shift.date <= end_of_week
    ).all()

    # 3. Process Data for Timeline View (Advanced Packing)
    events_by_activity = {}
    
    # --- A. Collect Logical Events (Full Duration) ---
    logical_events = []
    
    # Process Shifts
    for s in shifts:
        title = s.type if s.type else 'Guardia'
        s_date = s.date
        if s_date.weekday() >= 5: # Sat/Sun (9am - 9am next day)
             start_dt = datetime.combine(s_date, datetime.min.time()) + timedelta(hours=9)
             end_dt = start_dt + timedelta(hours=24)
        else: # Weekday (5pm - 8am next day)
             start_dt = datetime.combine(s_date, datetime.min.time()) + timedelta(hours=17)
             end_dt = start_dt + timedelta(hours=15)
             
        logical_events.append({
            'start_dt': start_dt,
            'end_dt': end_dt,
            'title': title, # Use title for Category grouping? Yes
            'ped_name': s.pediatrician.name,
            'color': '#3498db',
            'category': title,
            'conflict_id': None
        })

    # Process Activities
    for a in activities:
        a_type_name = a.activity_type.name if a.activity_type else (a.name or 'Evento')
        p_name = a.user.pediatrician.name if a.user.pediatrician else a.user.username
        
        # Determine Color
        color = '#3498db'
        is_conflict = False
        if a.id in conflict_ids:
            color = '#e74c3c'
            is_conflict = True
        elif a.activity_type_id and (a.start_time.date(), a.activity_type_id) in violation_keys:
             color = '#e74c3c'
             is_conflict = True
             
        logical_events.append({
            'start_dt': a.start_time,
            'end_dt': a.end_time,
            'title': a_type_name,
            'ped_name': p_name,
            'color': color,
            'category': a_type_name,
            'conflict_id': a.id if is_conflict else None
        })

    # --- B. Week-Based Packing (Assign Rows) ---
    # Group by category
    events_by_category = {}
    for e in logical_events:
        cat = e['category']
        if cat not in events_by_category: events_by_category[cat] = []
        events_by_category[cat].append(e)
        
    for cat, evts in events_by_category.items():
        # Sort by start time
        evts.sort(key=lambda x: x['start_dt'])
        
        # Map: DayString -> { RowIndex -> UserIdentifier }
        # Ensures that on any given day, a Row matches exactly one User.
        day_row_map = {}
        
        for e in evts:
            # 1. Identify Days Covered
            covered_days = []
            curr = e['start_dt']
            while curr < e['end_dt']:
                d_str = curr.strftime('%Y-%m-%d')
                covered_days.append(d_str)
                # Advance to next midnight
                curr = datetime.combine(curr.date() + timedelta(days=1), datetime.min.time())
            
            # 2. Find First Available Row
            user_id = e['ped_name']
            assigned_row = 0
            while True:
                conflict = False
                for d in covered_days:
                    if d not in day_row_map: day_row_map[d] = {}
                    
                    owner = day_row_map[d].get(assigned_row)
                    # Conflict if row is occupied by SOMEONE ELSE
                    if owner is not None and owner != user_id:
                        conflict = True
                        break
                
                if not conflict:
                    # Reserve this row for this user on all covered days
                    for d in covered_days:
                        if d not in day_row_map: day_row_map[d] = {}
                        day_row_map[d][assigned_row] = user_id
                    break
                else:
                    assigned_row += 1
            
            e['row_index'] = assigned_row

    # --- C. Split & Project to View ---
    for e in logical_events:
        curr_start = e['start_dt']
        end_dt = e['end_dt']
        
        # Track if this event spans multiple segments for blending
        # Actually blending flags depend on whether THIS segment is start or end of total
        total_duration = (end_dt - e['start_dt']).total_seconds()
        
        while curr_start < end_dt:
            next_midnight = datetime.combine(curr_start.date() + timedelta(days=1), datetime.min.time())
            segment_end = min(end_dt, next_midnight)
            
            d_str = curr_start.strftime('%Y-%m-%d')
            
            # Start/End/Width logic
            start_hour = curr_start.hour + (curr_start.minute / 60.0)
            duration_hours = (segment_end - curr_start).total_seconds() / 3600.0
            
            left_pct = (start_hour / 24.0) * 100
            width_pct = (duration_hours / 24.0) * 100
            
            # Continuity Flags
            is_start_of_event = (curr_start == e['start_dt'])
            is_end_of_event = (segment_end == e['end_dt'])
            
            if e['category'] not in events_by_activity: events_by_activity[e['category']] = {}
            if d_str not in events_by_activity[e['category']]: events_by_activity[e['category']][d_str] = []
            
            events_by_activity[e['category']][d_str].append({
                'pediatrician': e['ped_name'],
                'time_str': f"{e['start_dt'].strftime('%H:%M')} - {e['end_dt'].strftime('%H:%M')}", # Full duration string
                'color': e['color'],
                'left': left_pct,
                'width': width_pct,
                'top': e['row_index'] * 25,
                'row': e['row_index'],
                'blend_left': not is_start_of_event, # Remove Left border/radius
                'blend_right': not is_end_of_event   # Remove Right border/radius
            })
            
            curr_start = next_midnight

    # Calculate cell heights
    cell_heights = {}
    for cat, dates in events_by_activity.items():
        cell_heights[cat] = {}
        for d_str, evts in dates.items():
            max_row = 0
            if evts:
                max_row = max(e['row'] for e in evts)
            cell_heights[cat][d_str] = (max_row + 1) * 25 + 10

    return render_template('global_calendar.html', 
                           days=days, 
                           day_names=day_names,
                           week_offset=week_offset,
                           start_date=start_of_week,
                           end_date=end_of_week,
                           events_by_activity=events_by_activity,
                           cell_heights=cell_heights)

@app.route('/debug/validation')
@login_required
def debug_validation():
    from validation import check_overlap, get_validation_alerts
    from datetime import date, timedelta
    # Test Overlap
    # Find any activity for current user
    acts = Activity.query.filter_by(user_id=current_user.id).all()
    overlap_results = []
    for a in acts:
        # Check against itself (should be False if exclude works, but check_overlap without exclude checks existence)
        # Check explicit overlap logic
        is_ov = check_overlap(current_user.id, a.start_time, a.end_time, exclude_activity_id=a.id)
        overlap_results.append(f"Act {a.id} ({a.start_time} - {a.end_time}): Overlap? {is_ov}")

    # Test Staffing
    # Check current service alerts for today and next 7 days
    staffing_results = []
    today = date.today()
    for i in range(7):
        d = today + timedelta(days=i)
        alerts = get_validation_alerts(g.current_service.id, target_date=d)
        if alerts:
            staffing_results.append(f"Date {d}: {alerts}")
            
    return f"""
    <h1>Debug Validation</h1>
    <h2>User {current_user.id} ({current_user.username})</h2>
    <h3>Activities Found: {len(acts)}</h3>
    <pre>{chr(10).join([str(a) for a in acts])}</pre>
    <h3>Overlap Checks:</h3>
    <pre>{chr(10).join(overlap_results)}</pre>
    <h3>Staffing Alerts (Next 7 days):</h3>
    <pre>{chr(10).join(str(r) for r in staffing_results)}</pre>
    """

if __name__ == '__main__':
    # Initialize database before running the app
    init_db_and_seed()
    # The debug=True line should be removed for final deployment
    app.run(debug=True)
