from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta, date
import pandas as pd
from pulp import LpProblem, LpVariable, LpMinimize, LpStatus, LpBinary, lpSum, value
import logging

from app import app, db, Shift, Pediatrician, Preference, GlobalConfig

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
def get_config():
    defaults = {
        'S1': 2, 'S2': 2, 'M_START': 3, 'M_MIN': 1,
        'BALANCE_ALPHA': 1.0, 'USE_LEXICOGRAPHIC_FAIRNESS': True,
        'PENALTY_PREFER_NOT_DAY': 10, 'PENALTY_MISS_PREFERRED_DAY': 8,
        'PENALTY_EXCESS_WEEKLY_SHIFTS': 5, 'PENALTY_REPEATED_WEEKDAY': 30,
        'PENALTY_REPEATED_PAIRING': 35, 'PENALTY_MONTHLY_BALANCE': 60,
        'PENALTY_SHIFT_LIMIT_VIOLATION': 500, 'PENALTY_WEEKEND_LIMIT_VIOLATION': 400
    }
    config = defaults.copy()
    try:
        db_configs = GlobalConfig.query.all()
        for c in db_configs:
            if c.key in config:
                if c.value.lower() == 'true': config[c.key] = True
                elif c.value.lower() == 'false': config[c.key] = False
                else:
                    try:
                        if '.' in c.value: config[c.key] = float(c.value)
                        else: config[c.key] = int(c.value)
                    except: pass
    except: pass
    return config

# --- HELPER FUNCTIONS ---
COLUMN_NAMES = {
    'number': ['Number', 'Número', 'Num', 'Nombre'],
    'weekend': ['Weekend number', 'Fins de setmana', 'Fines de semana', 'Weekend'],
    'type': ['Type', 'Tipus', 'Tipo'],
    'mir': ['MIR', 'Resident'],
    'date': ['Date', 'Fecha', 'Data'],
    'reason': ['Reason', 'Razon', 'Motiu', 'Motivo'],
    'preference': ['Preference', 'Preferencia', 'Preferència']
}

def get_column_name(df, column_type):
    possible_names = COLUMN_NAMES[column_type]
    for name in possible_names:
        if name in df.columns: return name
    raise KeyError(f"Could not find any column matching {possible_names}")

def weekdays_to_dates(year, month, weekday_name):
    start_date = datetime(year, month, 1).date()
    if month == 12: end_date = datetime(year + 1, 1, 1).date() - timedelta(days=1)
    else: end_date = datetime(year, month + 1, 1).date() - timedelta(days=1)
    weekday_numbers = {'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3, 'friday': 4, 'saturday': 5, 'sunday': 6}
    weekday_key = weekday_name.strip().lower()
    if weekday_key not in weekday_numbers: return []
    weekday_num = weekday_numbers[weekday_key]
    return [start_date + timedelta(days=x) for x in range((end_date - start_date).days + 1) if (start_date + timedelta(days=x)).weekday() == weekday_num]

def expand_weekday_entries(df, date_col, year, month):
    expanded_rows = []
    for _, row in df.iterrows():
        date_entry = row[date_col]
        try:
            date = pd.to_datetime(date_entry).date()
            row[date_col] = date
            expanded_rows.append(row)
        except:
            weekday_name = str(date_entry).strip().capitalize()
            if weekday_name.lower() in ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']:
                for date in weekdays_to_dates(year, month, weekday_name):
                    new_row = row.copy()
                    new_row[date_col] = date
                    expanded_rows.append(new_row)
    if expanded_rows:
        result_df = pd.DataFrame(expanded_rows)
        result_df = result_df.reindex(columns=df.columns)
        return result_df
    return pd.DataFrame(columns=df.columns)

def process_month(year, month, xls, ped_sheets, ped_names, pediatricians):
    start_date = datetime(year, month, 1).date()
    if month == 12: end_date = datetime(year + 1, 1, 1).date() - timedelta(days=1)
    else: end_date = datetime(year, month + 1, 1).date() - timedelta(days=1)
    days = [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)]

    mandatory_shifts_df = pd.read_excel(xls, sheet_name='MandatoryShifts')
    date_col = get_column_name(mandatory_shifts_df, 'date')
    mandatory_shifts_df = expand_weekday_entries(mandatory_shifts_df, date_col, year, month)
    mandatory_shifts_df[date_col] = pd.to_datetime(mandatory_shifts_df[date_col]).dt.date

    mandatory_shifts = {}
    for _, row in mandatory_shifts_df.iterrows():
        ped_name = row['Pediatrician']
        shift_date = row[date_col]
        ped_id = next((pid for pid, name in ped_names.items() if name == ped_name), None)
        if ped_id and shift_date in days:
            mandatory_shifts.setdefault(ped_id, []).append(shift_date)

    vacation_days = {}
    skip_days = {}
    prefer_not_days = {}
    preferred_days = {}
    shift_limits = {}
    weekend_limits = {}
    tipus_status = {}
    mir_status = {}
    cannot_do_days = {}

    for i, sheet_name in enumerate(ped_sheets):
        df = pd.read_excel(xls, sheet_name=sheet_name)
        ped_id = i + 1
        
        number_col = get_column_name(df, 'number')
        weekend_col = get_column_name(df, 'weekend')
        type_col = get_column_name(df, 'type')
        mir_col = get_column_name(df, 'mir')
        date_col = get_column_name(df, 'date')
        reason_col = get_column_name(df, 'reason')
        pref_col = get_column_name(df, 'preference')

        shift_limits[ped_id] = {'min': df[number_col].iloc[0], 'max': df[number_col].iloc[1]}
        weekend_limits[ped_id] = {'min': df[weekend_col].iloc[0], 'max': df[weekend_col].iloc[1]}
        tipus_status[ped_id] = df[type_col].iloc[0]
        mir_status[ped_id] = (str(df[mir_col].iloc[0]).strip().lower() == 'yes')

        df = expand_weekday_entries(df, date_col, year, month)
        df[date_col] = pd.to_datetime(df[date_col]).dt.date

        vacation_days[ped_id] = set(df[df[reason_col] == 'Vacation'][date_col])
        skip_days[ped_id] = set(df[df[reason_col] == 'Skip'][date_col])
        prefer_not_days[ped_id] = set(df[df[pref_col] == 'Prefer Not'][date_col])
        preferred_days[ped_id] = set(df[df[pref_col] == 'Prefer'][date_col])
        congress_days = set(df[df[reason_col] == 'Congress'][date_col])

        cannot_work = set()
        for day in vacation_days[ped_id]: cannot_work.update({day, day - timedelta(days=1), day + timedelta(days=1)})
        for day in skip_days[ped_id]: cannot_work.add(day)
        for day in congress_days: cannot_work.update({day, day - timedelta(days=1), day + timedelta(days=1)})
        cannot_do_days[ped_id] = cannot_work

    for ped_id in mandatory_shifts:
        if ped_id in skip_days:
            skip_days_set = skip_days[ped_id]
            mandatory_shifts[ped_id] = [shift for shift in mandatory_shifts[ped_id] if shift not in skip_days_set]

    return {
        'days': days, 'start_date': start_date, 'end_date': end_date,
        'mandatory_shifts': mandatory_shifts, 'cannot_do_days': cannot_do_days,
        'prefer_not_days': prefer_not_days, 'preferred_days': preferred_days,
        'shift_limits': shift_limits, 'weekend_limits': weekend_limits,
        'tipus_status': tipus_status, 'mir_status': mir_status,
        'no_previous_day_shifts': {}
    }

def combine_month_with_overlap(year, month, M_overlap, xls, ped_sheets, ped_names, pediatricians):
    cur = process_month(year, month, xls, ped_sheets, ped_names, pediatricians)
    month_days = cur['days']
    overlap_days = []
    nxt = None
    if month < 12 and M_overlap > 0:
        nxt = process_month(year, month + 1, xls, ped_sheets, ped_names, pediatricians)
        overlap_days = nxt['days'][:M_overlap]
    
    days_all = month_days + overlap_days
    mandatory_all = {p: set(cur['mandatory_shifts'].get(p, [])) for p in pediatricians}
    if overlap_days and nxt:
        for p in pediatricians:
            mandatory_all[p].update(d for d in nxt['mandatory_shifts'].get(p, []) if d in overlap_days)
            
    cannot_all, prefer_not_all, preferred_all = {}, {}, {}
    for p in pediatricians:
        cannot_set = set(cur['cannot_do_days'].get(p, set()))
        prefer_not_set = set(cur['prefer_not_days'].get(p, set()))
        preferred_set = set(cur['preferred_days'].get(p, set()))
        if overlap_days and nxt:
            cannot_set.update(d for d in nxt['cannot_do_days'].get(p, set()) if d in overlap_days)
            prefer_not_set.update(d for d in nxt['prefer_not_days'].get(p, set()) if d in overlap_days)
            preferred_set.update(d for d in nxt['preferred_days'].get(p, set()) if d in overlap_days)
        cannot_all[p] = cannot_set
        prefer_not_all[p] = prefer_not_set
        preferred_all[p] = preferred_set

    return {
        'month_days': month_days, 'overlap_days': overlap_days, 'days_all': days_all,
        'start_date': cur['start_date'], 'end_date': cur['end_date'],
        'mandatory_all': mandatory_all, 'cannot_all': cannot_all,
        'prefer_not_all': prefer_not_all, 'preferred_all': preferred_all,
        'shift_limits_cur': cur['shift_limits'], 'weekend_limits_cur': cur['weekend_limits'],
        'shift_limits_next': (nxt['shift_limits'] if nxt else None),
        'weekend_limits_next': (nxt['weekend_limits'] if nxt else None),
        'tipus_status': cur['tipus_status'], 'mir_status': cur['mir_status'],
        'no_previous_day_shifts': cur['no_previous_day_shifts']
    }

def generate_and_save(start_year=2026, start_month=7, end_year=2026, end_month=12):
    with app.app_context():
        logger.info(f"Starting schedule generation for {start_year}/{start_month} to {end_year}/{end_month}")
        CONF = get_config()
        
        # Only clear shifts in the target date range

        logger.info(f"Cleaning existing shifts for {start_year}/{start_month} to {end_year}/{end_month}...")
        start_clean = datetime(start_year, start_month, 1).date()
        # Get last day of end_month
        if end_month == 12:
            end_clean = datetime(end_year, 12, 31).date()
        else:
            end_clean = (datetime(end_year, end_month + 1, 1) - timedelta(days=1)).date()
        
        Shift.query.filter(Shift.date >= start_clean, Shift.date <= end_clean).delete()
        db.session.commit()

        xls = pd.ExcelFile('year26.xlsx')
        ped_sheets = [sheet for sheet in xls.sheet_names if sheet != 'MandatoryShifts']
        ped_names = {i + 1: name for i, name in enumerate(ped_sheets)}
        pediatricians = list(ped_names.keys())
        
        # Ensure Peds exist
        for pid, name in ped_names.items():
            if not db.session.get(Pediatrician, pid):
                print(f"Adding missing pediatrician: {name} (ID={pid})")
                db.session.add(Pediatrician(id=pid, name=name))
                db.session.commit()

        prev_overlap_mandatory = {p: set() for p in pediatricians}
        cumulative_actual_free = {p: 0.0 for p in pediatricians}
        cumulative_target_free = {p: 0.0 for p in pediatricians}

        # Calculate month range
        current_year = start_year
        current_month = start_month
        
        while (current_year < end_year) or (current_year == end_year and current_month <= end_month):
            print(f"Generating {datetime(current_year, current_month, 1).strftime('%B %Y')}...")
            M_CANDIDATES = list(range(int(CONF['M_START']), int(CONF['M_MIN']) - 1, -1))
            
            used_M = None
            last_x = None
            last_data = None
            last_mandatory_cur = None
            
            for soft_phase in [False, True]:
                if used_M is not None: break
                
                for M_try in M_CANDIDATES:
                    data = combine_month_with_overlap(current_year, current_month, M_try, xls, ped_sheets, ped_names, pediatricians)
                    month_days = data['month_days']
                    days_all = data['days_all']
                    mandatory_all = data['mandatory_all']
                    cannot_all = data['cannot_all']
                    prefer_not_all = data['prefer_not_all']
                    preferred_all = data['preferred_all']
                    shift_limits_cur = data['shift_limits_cur']
                    weekend_limits_cur = data['weekend_limits_cur']
                    shift_limits_next = data['shift_limits_next']
                    weekend_limits_next = data['weekend_limits_next']
                    tipus_status = data['tipus_status']
                    mir_status = data['mir_status']
                    no_previous_day_shifts = data['no_previous_day_shifts']

                    mandatory_cur = {}
                    for p in pediatricians:
                        mand = set(d for d in mandatory_all.get(p, set()) if d in month_days)
                        mand |= set(d for d in prev_overlap_mandatory.get(p, set()) if d in month_days)
                        mandatory_cur[p] = len(mand)
                    last_mandatory_cur = mandatory_cur

                    desired_free = {}
                    for p in pediatricians:
                        mn = shift_limits_cur[p]['min']
                        mx = shift_limits_cur[p]['max']
                        midpoint_total = 0.5 * (mn + mx)
                        min_free = max(0.0, mn - mandatory_cur[p])
                        max_free = max(0.0, mx - mandatory_cur[p])
                        midpoint_free = max(0.0, midpoint_total - mandatory_cur[p])
                        imbalance_free = cumulative_target_free[p] - cumulative_actual_free[p]
                        d_free = midpoint_free + CONF['BALANCE_ALPHA'] * imbalance_free
                        desired_free[p] = max(min_free, min(max_free, d_free))

                    prob = LpProblem(f"Solve_{current_month}_{M_try}", LpMinimize)
                    x = LpVariable.dicts("Shift", ((p, d) for p in pediatricians for d in days_all), cat=LpBinary)
                    week_violations = LpVariable.dicts("WeekViolation", ((p, w) for p in pediatricians for w in range(5)), cat=LpBinary)
                    missed_preferred = LpVariable.dicts("MissedPreferred", ((p, d) for p in pediatricians for d in days_all), cat=LpBinary)
                    pair_violations = LpVariable.dicts("PairViolation", ((p1, p2) for p1 in pediatricians for p2 in pediatricians if p1 < p2), cat=LpBinary)
                    working_together = LpVariable.dicts("WorkingTogether", ((p1, p2, d) for p1 in pediatricians for p2 in pediatricians for d in days_all if p1 < p2), cat=LpBinary)
                    bal_dev_pos = LpVariable.dicts("BalDevPos", pediatricians, lowBound=0)
                    bal_dev_neg = LpVariable.dicts("BalDevNeg", pediatricians, lowBound=0)
                    
                    if soft_phase:
                        under_min = LpVariable.dicts("UnderMinShifts", pediatricians, lowBound=0)
                        over_max = LpVariable.dicts("OverMaxShifts", pediatricians, lowBound=0)
                        under_min_wknd = LpVariable.dicts("UnderMinWeekend", pediatricians, lowBound=0)
                        over_max_wknd = LpVariable.dicts("OverMaxWeekend", pediatricians, lowBound=0)

                    penalty_terms_base = []
                    penalty_terms_fair = []

                    for p in pediatricians:
                        for d in days_all:
                            if d in prefer_not_all.get(p, set()):
                                penalty_terms_base.append(CONF['PENALTY_PREFER_NOT_DAY'] * x[p, d])
                            if d in preferred_all.get(p, set()):
                                prob += missed_preferred[p, d] >= 1 - x[p, d]
                                penalty_terms_base.append(CONF['PENALTY_MISS_PREFERRED_DAY'] * missed_preferred[p, d])

                    for p in pediatricians:
                        num_weeks = (len(month_days) + 6) // 7
                        for week in range(num_weeks):
                            week_start = week * 7
                            week_end = min(week_start + 7, len(month_days))
                            week_days = month_days[week_start:week_end]
                            if week_days:
                                penalty_terms_base.append(CONF['PENALTY_EXCESS_WEEKLY_SHIFTS'] * week_violations[p, week])
                                total_week_shifts = lpSum(x[p, d] for d in week_days)
                                average_shifts = (total_week_shifts - 2) / len(week_days)
                                prob += week_violations[p, week] >= average_shifts

                    for p in pediatricians:
                        for dow in range(7):
                            same_dow_days = [d for d in month_days if d.weekday() == dow]
                            if len(same_dow_days) >= 2:
                                for i in range(1, len(same_dow_days)):
                                    penalty_terms_base.append(CONF['PENALTY_REPEATED_WEEKDAY'] * (lpSum(x[p, same_dow_days[j]] for j in range(i + 1)) - 1))

                    for p1 in pediatricians:
                        for p2 in pediatricians:
                            if p1 < p2:
                                for d in days_all:
                                    prob += working_together[p1, p2, d] >= x[p1, d] + x[p2, d] - 1
                                    prob += working_together[p1, p2, d] <= x[p1, d]
                                    prob += working_together[p1, p2, d] <= x[p2, d]
                                prob += pair_violations[p1, p2] >= (lpSum(working_together[p1, p2, d] for d in days_all) - 1) / len(days_all)
                                penalty_terms_base.append(CONF['PENALTY_REPEATED_PAIRING'] * pair_violations[p1, p2])

                    if soft_phase:
                        for p in pediatricians:
                            penalty_terms_base.append(CONF['PENALTY_SHIFT_LIMIT_VIOLATION'] * (under_min[p] + over_max[p]))
                            penalty_terms_base.append(CONF['PENALTY_WEEKEND_LIMIT_VIOLATION'] * (under_min_wknd[p] + over_max_wknd[p]))

                    for p in pediatricians:
                        month_total = lpSum(x[p, d] for d in month_days)
                        month_free = month_total - mandatory_cur[p]
                        prob += bal_dev_pos[p] >= month_free - desired_free[p]
                        prob += bal_dev_neg[p] >= desired_free[p] - month_free
                        penalty_terms_fair.append(CONF['PENALTY_MONTHLY_BALANCE'] * (bal_dev_pos[p] + bal_dev_neg[p]))

                    base_expr = lpSum(penalty_terms_base)
                    fair_expr = lpSum(penalty_terms_fair)

                    if CONF['USE_LEXICOGRAPHIC_FAIRNESS']: prob += base_expr
                    else: prob += base_expr + fair_expr

                    # Constraints
                    for p in pediatricians:
                        for d in mandatory_all.get(p, set()): prob += x[p, d] == 1
                        for d in prev_overlap_mandatory.get(p, set()): 
                            if d in month_days: prob += x[p, d] == 1
                        for d in days_all:
                            if d in cannot_all.get(p, set()): prob += x[p, d] == 0
                        
                        if p in no_previous_day_shifts:
                            for d in no_previous_day_shifts[p]:
                                prev_day = d - timedelta(days=1)
                                if prev_day in month_days: prob += x[p, prev_day] == 0

                        weekend_days_cur = [d for d in month_days if d.weekday() >= 5]
                        wknd_total = lpSum(x[p, d] for d in weekend_days_cur)
                        if soft_phase:
                            prob += wknd_total + under_min_wknd[p] >= weekend_limits_cur[p]['min']
                            prob += wknd_total - over_max_wknd[p] <= weekend_limits_cur[p]['max']
                        else:
                            prob += wknd_total >= weekend_limits_cur[p]['min']
                            prob += wknd_total <= weekend_limits_cur[p]['max']

                        month_total = lpSum(x[p, d] for d in month_days)
                        if soft_phase:
                            prob += month_total + under_min[p] >= shift_limits_cur[p]['min']
                            prob += month_total - over_max[p] <= shift_limits_cur[p]['max']
                        else:
                            prob += month_total >= shift_limits_cur[p]['min']
                            prob += month_total <= shift_limits_cur[p]['max']

                    if data['overlap_days'] and shift_limits_next and weekend_limits_next:
                        for p in pediatricians:
                            prob += lpSum(x[p, d] for d in data['overlap_days']) <= shift_limits_next[p]['max']
                            overlap_weekends = [d for d in data['overlap_days'] if d.weekday() >= 5]
                            if overlap_weekends:
                                prob += lpSum(x[p, d] for d in overlap_weekends) <= weekend_limits_next[p]['max']

                    for p in pediatricians:
                        for d in days_all:
                            for k in range(1, M_try + 1):
                                next_day = d + timedelta(days=k)
                                if next_day in days_all: prob += x[p, d] + x[p, next_day] <= 1

                    for d in days_all:
                        prob += lpSum(x[p, d] for p in pediatricians) >= CONF['S1']
                        prob += lpSum(x[p, d] for p in pediatricians) <= CONF['S2']

                    residents = [p for p, t in tipus_status.items() if t == 'Resident']
                    non_mir_supervisors = [p for p, can_supervise in mir_status.items() if not can_supervise]
                    for d in days_all:
                        for resident in residents:
                            for non_mir in non_mir_supervisors:
                                prob += x[resident, d] + x[non_mir, d] <= 1
                        prob += lpSum(x[p, d] for p in residents) <= 1

                    # Dispose of the connection pool to force reconnection after long solve
                    # This prevents "MySQL server has gone away" errors
                    db.engine.dispose()
                    
                    prob.solve()
                    if LpStatus[prob.status] == 'Optimal':
                        if CONF['USE_LEXICOGRAPHIC_FAIRNESS']:
                            base_val = value(base_expr)
                            prob += base_expr <= base_val + 1e-6
                            prob.objective = fair_expr
                            prob.solve()
                        used_M = M_try
                        last_x = x
                        last_data = data
                        break
            
            if last_x:
                month_str = datetime(current_year, current_month, 1).strftime('%B %Y')
                logger.info(f"Saving shifts for {month_str}...")
                
                # Refresh the session to ensure we have a fresh connection
                db.session.rollback()  # Clear any pending state
                
                shifts_to_add = []
                for p in pediatricians:
                    for d in data['month_days']:
                        if last_x[p, d].varValue == 1:
                            shifts_to_add.append(Shift(pediatrician_id=p, date=d))
                
                logger.info(f"Attempting to add {len(shifts_to_add)} shifts for {month_str}...")
                
                # Check for duplicates in shifts_to_add
                seen = set()
                for s in shifts_to_add:
                    key = (s.pediatrician_id, s.date)
                    if key in seen:
                        logger.warning(f"DUPLICATE IN MEMORY: {key}")
                    seen.add(key)
                
                try:
                    db.session.add_all(shifts_to_add)
                    db.session.commit()
                    logger.info(f"Successfully saved {len(shifts_to_add)} shifts for {month_str}")
                except Exception as e:
                    logger.error(f"!!! ERROR SAVING SHIFTS FOR {month_str} !!!")
                    logger.error(f"Exception type: {type(e).__name__}")
                    logger.error(f"Exception message: {str(e)[:500]}")
                    if hasattr(e, 'orig'):
                        logger.error(f"Original error: {e.orig}")
                    db.session.rollback()
                    return # Stop on error
                
                # Update trackers
                for p in pediatricians:
                    assigned_total = len([d for d in data['month_days'] if last_x[p, d].varValue == 1])
                    mandatory_p = last_mandatory_cur[p] if last_mandatory_cur else 0
                    assigned_free = max(0.0, assigned_total - mandatory_p)
                    mn = shift_limits_cur[p]['min']
                    mx = shift_limits_cur[p]['max']
                    midpoint_total = 0.5 * (mn + mx)
                    target_free = max(0.0, midpoint_total - mandatory_p)
                    cumulative_actual_free[p] += assigned_free
                    cumulative_target_free[p] += target_free
                
                new_prev_overlap = {p: set() for p in pediatricians}
                if data['overlap_days']:
                    for p in pediatricians:
                        new_prev_overlap[p] = {d for d in data['overlap_days'] if last_x[p, d].varValue == 1}
                prev_overlap_mandatory = new_prev_overlap
            else:
                print(f"FAILED to solve for {datetime(current_year, current_month, 1).strftime('%B %Y')}")
            
            # Increment month
            current_month += 1
            if current_month > 12:
                current_month = 1
                current_year += 1
        
        logger.info("Schedule generation completed successfully!")

if __name__ == '__main__':
    try:
        generate_and_save()
    except Exception as e:
        logger.error("Fatal error during schedule generation:")
        import traceback
        traceback.print_exc()
