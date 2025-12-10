from datetime import datetime, date, timedelta
from collections import defaultdict

def get_service_alerts(service_id, start_date, end_date):
    """
    Efficiently checks for Validation Alerts within a date range.
    Returns:
    {
        'overlaps': [ {user, date, activities: [], message} ],
        'staffing': [ {date, type, count, min, max, message, level} ]
    }
    """
    from app import db, ActivityType, Activity, User, Pediatrician

    alerts = {
        'overlaps': [],
        'staffing': []
    }

    # 1. Fetch All Activities in Range
    activities = Activity.query.join(User).join(Pediatrician).filter(
        Pediatrician.service_id == service_id,
        Activity.start_time >= datetime.combine(start_date, datetime.min.time()),
        Activity.end_time <= datetime.combine(end_date, datetime.max.time())
    ).order_by(Activity.user_id, Activity.start_time).all()

    # 2. Check Overlaps (Linear Scan per User)
    activities_by_user = defaultdict(list)
    for a in activities:
        activities_by_user[a.user_id].append(a)

    for user_id, user_acts in activities_by_user.items():
        for i in range(len(user_acts) - 1):
            a1 = user_acts[i]
            a2 = user_acts[i+1]
            
            if a1.end_time > a2.start_time:
                user_name = a1.user.pediatrician.name if a1.user.pediatrician else a1.user.username
                msg = f"Incompatibilidad: {user_name} tiene simul.{a1.start_time.strftime('%H:%M')} y {a2.start_time.strftime('%H:%M')}"
                alerts['overlaps'].append({
                    'user': user_name,
                    'date': a1.start_time.date(),
                    'activities': [a1, a2],
                    'message': msg
                })

    # 3. Check Staffing Levels (Min/Max)
    act_types = ActivityType.query.filter_by(service_id=service_id).all()
    limits = {at.id: {'min': at.min_staff, 'max': at.max_staff, 'name': at.name} for at in act_types}
    
    daily_counts = defaultdict(set)
    for a in activities:
        if a.activity_type_id in limits:
            d_key = (a.start_time.date(), a.activity_type_id)
            daily_counts[d_key].add(a.user_id)
            
    for (day, type_id), users in daily_counts.items():
        count = len(users)
        rule = limits[type_id]
        
        if rule['min'] is not None and count < rule['min']:
            alerts['staffing'].append({
                'date': day,
                'type': rule['name'],
                'count': count,
                'min': rule['min'],
                'message': f"Falta personal en {rule['name']} ({count}/{rule['min']})",
                'level': 'error'
            })
            
        if rule['max'] is not None and count > rule['max']:
             alerts['staffing'].append({
                'date': day,
                'type': rule['name'],
                'count': count,
                'max': rule['max'],
                'message': f"Exceso de personal en {rule['name']} ({count}/{rule['max']})",
                'level': 'warning'
            })

    return alerts

def get_validation_alerts(service_id, target_date=None):
    if target_date is None: target_date = date.today()
    res = get_service_alerts(service_id, target_date, target_date)
    flat = []
    for s in res['staffing']:
        flat.append({'type': 'staffing', 'level': s['level'], 'message': s['message']})
    for o in res['overlaps']:
        flat.append({'type': 'overlap', 'level': 'error', 'message': o['message']})
    return flat
    
def check_overlap(user_id, start_time, end_time, exclude_activity_id=None):
    from app import Activity
    query = Activity.query.filter(
        Activity.user_id == user_id,
        Activity.start_time < end_time,
        Activity.end_time > start_time
    )
    if exclude_activity_id:
        query = query.filter(Activity.id != exclude_activity_id)
    return query.first() is not None

def check_max_staff_limit(activity_type_id, target_date, exclude_user_id):
    from app import db, ActivityType, Activity
    at = ActivityType.query.get(activity_type_id)
    if not at or at.max_staff is None:
        return False, None, 0
    current_count = db.session.query(Activity.user_id).filter(
        Activity.activity_type_id == activity_type_id,
        db.func.date(Activity.start_time) == target_date,
        Activity.user_id != exclude_user_id
    ).distinct().count()
    if current_count >= at.max_staff:
        return True, at.max_staff, current_count
    return False, at.max_staff, current_count
