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


    from app import db, ActivityType, Activity, User, Pediatrician, Shift

    alerts = {
        'overlaps': [],
        'staffing': []
    }

    # 1. Fetch All Activities & Shifts in Range
    # Use Activity.user criteria to match Global Calendar logic (active_service_id)
    activities = Activity.query.filter(
        Activity.user.has(active_service_id=service_id),
        Activity.start_time >= datetime.combine(start_date, datetime.min.time()),
        Activity.end_time <= datetime.combine(end_date, datetime.max.time())
    ).all()

    shifts = Shift.query.join(Pediatrician).filter(
        Pediatrician.service_id == service_id,
        Shift.date >= start_date,
        Shift.date <= end_date
    ).all()

    # Helper for unified objects
    class TimelineObj:
        def __init__(self, user_id, start, end, name, obj_type, user_display_name):
            self.user_id = user_id
            self.start_time = start
            self.end_time = end
            self.name = name
            self.type = obj_type
            self.user_name = user_display_name

    items_by_user = defaultdict(list)

    # Process Activities
    for a in activities:
        user = a.user
        # Display name: Ped Name if avail, else Username
        u_name = user.pediatrician.name if user.pediatrician else user.username
        name = a.activity_type.name if a.activity_type else (a.name or 'Activity')
        obj = TimelineObj(user.id, a.start_time, a.end_time, name, 'Activity', u_name)
        items_by_user[user.id].append(obj)

    # Process Shifts (Convert to Time Range & Map to User)
    for s in shifts:
        s_date = s.date
        title = s.type if s.type else 'Guardia'
        if s_date.weekday() >= 5: # Weekend (Sat/Sun)
             start_dt = datetime.combine(s_date, datetime.min.time()) + timedelta(hours=9)
             end_dt = start_dt + timedelta(hours=24)
        else: # Weekday
             start_dt = datetime.combine(s_date, datetime.min.time()) + timedelta(hours=17)
             end_dt = start_dt + timedelta(hours=15)
        
        # Map Shift (Pediatrician) -> User(s)
        # Because we compare with Activities (User-based), we must map Shift -> User ID
        linked_users = s.pediatrician.users
        for u in linked_users:
            # Inherit name from Pediatrician for shifts
            u_name = s.pediatrician.name 
            obj = TimelineObj(u.id, start_dt, end_dt, title, 'Shift', u_name)
            items_by_user[u.id].append(obj)

    # 2. Check Overlaps (Exhaustive Check per User)
    for user_id, items in items_by_user.items():
        n = len(items)
        for i in range(n):
            for j in range(i + 1, n):
                o1 = items[i]
                o2 = items[j]
                
                # Overlap conditions
                if o1.start_time < o2.end_time and o2.start_time < o1.end_time:
                     msg = f"Incompatibilidad: {o1.user_name} - {o1.name} ({o1.start_time.strftime('%H:%M')}-{o1.end_time.strftime('%H:%M')}) coincide con {o2.name} ({o2.start_time.strftime('%H:%M')}-{o2.end_time.strftime('%H:%M')})"
                     
                     alerts['overlaps'].append({
                        'user': o1.user_name,
                        'date': o1.start_time.date(),
                        'activities': [], 
                        'message': msg
                     })

    # 3. Check Staffing Levels (Min/Max) - GRANULAR (Sweep Line)
    act_types = ActivityType.query.filter_by(service_id=service_id).all()
    
    # Organized: Activities by (Date, TypeID)
    acts_by_date_type = defaultdict(list)
    for a in activities:
        d = a.start_time.date()
        acts_by_date_type[(d, a.activity_type_id)].append(a)
    
    # To catch "Empty Days", we iterate the actual date range
    curr_date = start_date
    while curr_date <= end_date:
        for at in act_types:
            min_s = at.min_staff
            max_s = at.max_staff
            
            if min_s is None and max_s is None:
                continue

            day_acts = acts_by_date_type.get((curr_date, at.id), [])
            
            if not day_acts:
                # 0 Staff case
                if min_s is not None and min_s > 0:
                     # Heuristic: Skip weekends if not typical? Use user feedback if noisy.
                     alerts['staffing'].append({
                        'date': curr_date,
                        'type': at.name,
                        'count': 0,
                        'min': min_s,
                        'message': f"Ausencia total en {at.name} (Req: {min_s})",
                        'level': 'error'
                     })
                continue

            # Sweep Line for Granular Count
            points = []
            for a in day_acts:
                points.append((a.start_time, 1))
                points.append((a.end_time, -1))
                
            points.sort(key=lambda x: x[0])
            
            current_staff = 0
            for i in range(len(points) - 1):
                time_pt, change = points[i]
                current_staff += change
                
                next_time = points[i+1][0]
                
                if next_time > time_pt:
                    if min_s is not None and current_staff < min_s:
                         alerts['staffing'].append({
                            'date': curr_date,
                            'type': at.name,
                            'count': current_staff,
                            'min': min_s,
                            'message': f"Falta personal en {at.name} ({current_staff}/{min_s}) de {time_pt.strftime('%H:%M')} a {next_time.strftime('%H:%M')}",
                            'level': 'error'
                         })
                    
                    if max_s is not None and current_staff > max_s:
                         alerts['staffing'].append({
                            'date': curr_date,
                            'type': at.name,
                            'count': current_staff,
                            'max': max_s,
                            'message': f"Exceso en {at.name} ({current_staff}/{max_s}) de {time_pt.strftime('%H:%M')} a {next_time.strftime('%H:%M')}",
                            'level': 'warning'
                         })
        
        curr_date += timedelta(days=1)

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
