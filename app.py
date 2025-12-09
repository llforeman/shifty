from flask import Flask, render_template, request, redirect, url_for, abort, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import date, datetime, timedelta
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
    email = db.Column(db.String(120), unique=True, nullable=True)
    password_hash = db.Column(db.String(255))
    role = db.Column(db.String(50), default='user') # 'manager' or 'user'
    must_change_password = db.Column(db.Boolean, default=False)
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

class GlobalConfig(db.Model):
    __tablename__ = 'global_config'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.String(255), nullable=False)

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
    name = db.Column(db.String(100), unique=True, nullable=False)
    
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
    activity_types = ActivityType.query.order_by(ActivityType.name).all()

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
                target_date = start_date + timedelta(days=act.recurrence_day) # ERROR: recurrence_day might be mismatch with start_date weekday base.
                # Correction: We need to find the date corresponding to act.recurrence_day (0-6) within [start_date, end_date]
                
                # act.recurrence_day is 0-6. start_date is Monday (0).
                # So if start_date is Mon(0) and recurrence_day is 0, target is start_date + 0.
                # If recurrence_day is 2 (Wed), target is start_date + 2.
                # Since start_date is always Monday (as per logic above: today - timedelta(days=today.weekday())), this simple addition works.
                
                target_date = start_date + timedelta(days=act.recurrence_day)
                
                # Check end date
                if (not act.recurrence_end_date or target_date <= act.recurrence_end_date) and target_date >= act.start_time.date():
                    if target_date not in exceptions:
                        act_dates.append(target_date)
                    
            for d in act_dates:
                day_idx = (d - start_date).days
                s_hour = act.start_time.hour
                e_hour = act.end_time.hour + (act.end_time.minute / 60.0)
                if e_hour == 0: e_hour = 24 # Handle midnight end
                
                # Helper to format time strings for the form
                start_iso = datetime.combine(d, act.start_time.time()).strftime('%Y-%m-%dT%H:%M')
                end_iso = datetime.combine(d, act.end_time.time()).strftime('%Y-%m-%dT%H:%M')
                if e_hour == 24: # Correction for ISO string if it ends at midnight next day
                     end_iso = (datetime.combine(d, datetime.min.time()) + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
    
                events_by_day[day_idx].append({
                    'id': act.id,
                    'title': act.activity_type.name if act.activity_type else (act.name or 'Unknown'),
                    'place': act.place,
                    'start_hour': s_hour,
                    'end_hour': e_hour,
                    'type': 'activity',
                    'color': '#4299e1', # Blue
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
                        clusters.append(current_cluster)
                        current_cluster = [evt]
                        cluster_end = evt['end_hour']
            if current_cluster:
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
        # In production, flash error
        
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
def seed_global_config():
    """Seeds the database with default configuration values and handles schema updates."""
    # 1. Schema Migration: Check for recipient_id in chat_message
    try:
        inspector = db.inspect(db.engine)
        columns = [c['name'] for c in inspector.get_columns('chat_message')]
        if 'recipient_id' not in columns:
            print("Migrating: Adding recipient_id to chat_message...")
            with db.engine.connect() as conn:
                conn.execute(db.text("ALTER TABLE chat_message ADD COLUMN recipient_id INTEGER"))
                # SQLite doesn't support adding FK in ALTER easily, but MySQL does.
                # For safety/compatibility we might skip FK constraint or try it.
                # conn.execute(db.text("ALTER TABLE chat_message ADD CONSTRAINT fk_chat_recipient FOREIGN KEY (recipient_id) REFERENCES user(id)"))
                conn.commit()
            print("Migration successful: recipient_id added.")
    except Exception as e:
        print(f"Migration check failed (safe to ignore if table doesn't exist yet): {e}")

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
    pediatricians = Pediatrician.query.order_by(Pediatrician.name).all()
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
    
@app.route('/admin/create_user', methods=['GET', 'POST'])
@login_required
@role_required('manager')
def admin_create_user():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        role = request.form.get('role')
        
        # Validation
        if User.query.filter_by(email=email).first():
            flash('El email ya está registrado.', 'error')
            return redirect(url_for('admin_create_user'))
            
        try:
            # 1. Handle Pediatrician (if role is user)
            ped_id = None
            if role == 'user':
                existing_ped = Pediatrician.query.filter_by(name=name).first()
                if existing_ped:
                    ped_id = existing_ped.id
                else:
                    # Create new Pediatrician
                    new_ped = Pediatrician(name=name)
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
                must_change_password=True
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
            
    return render_template('admin_create_user.html')


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
    
    # REMOVED: Regular users only see their own shifts logic. 
    # Now all users see all shifts to enable swapping.
    
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
                           month_calendar=cal, shifts=shifts_by_day,
                           prev_year=prev_year, prev_month=prev_month,
                           next_year=next_year, next_month=next_month,
                           next_shift_date=next_shift_date,
                           is_draft=is_draft,
                           current_user=current_user)

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
    activity_types = ActivityType.query.order_by(ActivityType.name).all()
    return render_template('admin_activity_types.html', activity_types=activity_types)

@app.route('/admin/activity_types/add', methods=['POST'])
@login_required
@role_required('manager')
def admin_add_activity_type():
    name = request.form.get('name')
    if name:
        if not ActivityType.query.filter_by(name=name).first():
            db.session.add(ActivityType(name=name))
            db.session.commit()
    return redirect(url_for('admin_activity_types_page'))

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
    

if __name__ == '__main__':
    # Initialize database before running the app
    init_db_and_seed()
    # The debug=True line should be removed for final deployment
    app.run(debug=True)
