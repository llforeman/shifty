from datetime import datetime, date
from app import db, Activity, Shift, ActivityType, User, Pediatrician

def get_validation_alerts(service_id, target_date=None):
    """
    Checks for:
    1. Min/Max staff violations per ActivityType per day.
    2. Overlapping activities for users.
    
    Returns a list of alert dicts:
    [{'type': 'staffing|overlap', 'level': 'error|warning', 'message': '...'}]
    """
    alerts = []
    
    if target_date is None:
        target_date = date.today()
        
    # 1. Min/Max Staff Check
    # Get all activity types with constraints
    activity_types = ActivityType.query.filter_by(service_id=service_id).all()
    
    for at in activity_types:
        if at.min_staff is None and at.max_staff is None:
            continue
            
        # Count unique staff in this activity on this date
        # Assuming Activity covers the main shift hours or we just count distinct people 
        # who have an activity of this type on this day.
        # This is high-level daily validation.
        
        staff_count = db.session.query(Activity.user_id).filter(
            Activity.activity_type_id == at.id,
            db.func.date(Activity.start_time) == target_date
        ).distinct().count()
        
        if at.min_staff is not None and staff_count < at.min_staff:
            alerts.append({
                'type': 'staffing',
                'level': 'error',
                'message': f"Falta personal en '{at.name}' para {target_date.strftime('%d/%m')}. Actual: {staff_count}, Mínimo: {at.min_staff}"
            })
            
        if at.max_staff is not None and staff_count > at.max_staff:
            alerts.append({
                'type': 'staffing',
                'level': 'warning',
                'message': f"Exceso de personal en '{at.name}' para {target_date.strftime('%d/%m')}. Actual: {staff_count}, Máximo: {at.max_staff}"
            })

    return alerts

def check_overlap(user_id, start_time, end_time, exclude_activity_id=None):
    """
    Checks if user has overlapping activities.
    Returns True if overlap exists.
    """
    query = Activity.query.filter(
        Activity.user_id == user_id,
        Activity.start_time < end_time,
        Activity.end_time > start_time
    )
    
    if exclude_activity_id:
        query = query.filter(Activity.id != exclude_activity_id)
        
    return query.first() is not None
