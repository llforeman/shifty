import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from datetime import datetime, timedelta
from pulp import LpProblem, LpVariable, LpMinimize, LpStatus, LpBinary, lpSum, value
import os

# -----------------------------
# Configuration and Constants
# -----------------------------
PENALTY_WEIGHTS = {
    'PREFER_NOT_DAY': 10,
    'MISS_PREFERRED_DAY': 8,
    'EXCESS_WEEKLY_SHIFTS': 5,
    'REPEATED_WEEKDAY': 30,
    'REPEATED_PAIRING': 35,

    # Fairness (soft, low priority in lexicographic solve)
    'MONTHLY_BALANCE': 60,

    # Soft violations for min/max (only used in second phase)
    'SHIFT_LIMIT_VIOLATION': 500,
    'WEEKEND_LIMIT_VIOLATION': 400,
}

BALANCE_ALPHA = 1.0

USE_LEXICOGRAPHIC_FAIRNESS = True
LEXI_TOL = 1e-6

COLUMN_NAMES = {
    'number': ['Number', 'Número', 'Num', 'Nombre'],
    'weekend': ['Weekend number', 'Fins de setmana', 'Fines de semana', 'Weekend'],
    'type': ['Type', 'Tipus', 'Tipo'],
    'mir': ['MIR', 'Resident'],
    'date': ['Date', 'Fecha', 'Data'],
    'reason': ['Reason', 'Razon', 'Motiu', 'Motivo'],
    'preference': ['Preference', 'Preferencia', 'Preferència']
}

S1 = 2  # min pediatricians/day
S2 = 2  # max pediatricians/day

M_START = 3
M_MIN = 1
M_CANDIDATES = list(range(M_START, M_MIN - 1, -1))  # [3,2,1]

# -----------------------------
# Helper Functions
# -----------------------------
def get_column_name(df, column_type):
    possible_names = COLUMN_NAMES[column_type]
    for name in possible_names:
        if name in df.columns:
            return name
    raise KeyError(f"Could not find any column matching {possible_names}")

def weekdays_to_dates(year, month, weekday_name):
    start_date = datetime(year, month, 1).date()
    if month == 12:
        end_date = datetime(year + 1, 1, 1).date() - timedelta(days=1)
    else:
        end_date = datetime(year, month + 1, 1).date() - timedelta(days=1)
    weekday_numbers = {
        'monday': 0, 'tuesday': 1, 'wednesday': 2,
        'thursday': 3, 'friday': 4, 'saturday': 5, 'sunday': 6
    }
    weekday_key = weekday_name.strip().lower()
    if weekday_key not in weekday_numbers:
        raise ValueError(f"Invalid weekday name: {weekday_name}")
    weekday_num = weekday_numbers[weekday_key]
    return [start_date + timedelta(days=x)
            for x in range((end_date - start_date).days + 1)
            if (start_date + timedelta(days=x)).weekday() == weekday_num]

def expand_weekday_entries(df, date_col, year, month):
    expanded_rows = []
    for _, row in df.iterrows():
        date_entry = row[date_col]
        try:
            date = pd.to_datetime(date_entry).date()
            row[date_col] = date
            expanded_rows.append(row)
        except (ValueError, TypeError):
            weekday_name = str(date_entry).strip().capitalize()
            if weekday_name.lower() in ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']:
                for date in weekdays_to_dates(year, month, weekday_name):
                    new_row = row.copy()
                    new_row[date_col] = date
                    expanded_rows.append(new_row)
            else:
                print(f"Warning: '{date_entry}' is neither a date nor a valid weekday name.")
    if expanded_rows:
        result_df = pd.DataFrame(expanded_rows)
        result_df = result_df.reindex(columns=df.columns, fill_value=None)
        return result_df
    else:
        return pd.DataFrame(columns=df.columns)

def print_feasibility_summary(shift_limits, weekend_limits, days, S1, S2):
    total_min_shifts = sum(shift_limits[p]['min'] for p in shift_limits)
    total_max_shifts = sum(shift_limits[p]['max'] for p in shift_limits)
    total_min_weekend = sum(weekend_limits[p]['min'] for p in weekend_limits)
    total_max_weekend = sum(weekend_limits[p]['max'] for p in weekend_limits)

    total_required = len(days) * S1
    weekends = [d for d in days if d.weekday() >= 5]
    weekend_required = len(weekends) * S1

    print("SHIFT CAPACITY ASSESSMENT:")

    overall_min_margin = (total_min_shifts / total_required - 1) * 100
    overall_max_margin = (total_max_shifts / total_required - 1) * 100

    print(f"Overall minimum: {total_min_shifts:.0f}/{total_required} ({overall_min_margin:+.1f}%) - ", end="")
    if overall_min_margin >= 0:
        print("X INCORRECT")
    elif overall_min_margin < -5:
        print("OK ADEQUATE")
    else:
        print("! TIGHT")

    print(f"Overall maximum: {total_max_shifts:.0f}/{total_required} ({overall_max_margin:+.1f}%) - ", end="")
    if overall_max_margin > 15:
        print("OK ADEQUATE")
    else:
        print("! TIGHT")

    weekend_min_margin = (total_min_weekend / weekend_required - 1) * 100
    weekend_max_margin = (total_max_weekend / weekend_required - 1) * 100

    print(f"Weekend minimum: {total_min_weekend:.0f}/{weekend_required} ({weekend_min_margin:+.1f}%) - ", end="")
    if weekend_min_margin >= 0:
        print("X INCORRECT")
    elif weekend_min_margin < -5:
        print("OK ADEQUATE")
    else:
        print("! TIGHT")

    print(f"Weekend maximum: {total_max_weekend:.0f}/{weekend_required} ({weekend_max_margin:+.1f}%) - ", end="")
    if weekend_max_margin > 15:
        print("OK ADEQUATE")
    else:
        print("! TIGHT")

    print()
    return {
        'total_min_shifts': total_min_shifts,
        'total_max_shifts': total_max_shifts,
        'total_required': total_required,
        'overall_min_margin': overall_min_margin,
        'overall_max_margin': overall_max_margin,
        'total_min_weekend': total_min_weekend,
        'total_max_weekend': total_max_weekend,
        'weekend_required': weekend_required,
        'weekend_min_margin': weekend_min_margin,
        'weekend_max_margin': weekend_max_margin
    }

# -----------------------------
# Month Data Preparation
# -----------------------------
def process_month(year, month, xls, ped_sheets, ped_names, pediatricians):
    start_date = datetime(year, month, 1).date()
    if month == 12:
        end_date = datetime(year + 1, 1, 1).date() - timedelta(days=1)
    else:
        end_date = datetime(year, month + 1, 1).date() - timedelta(days=1)
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
    cannot_do_days = {}
    congress_days = {}
    skip_days = {}
    prefer_not_days = {}
    preferred_days = {}
    shift_limits = {}
    weekend_limits = {}
    tipus_status = {}
    mir_status = {}

    no_previous_day_config = {}
    no_previous_day_shifts = {}
    for ped, weekdays in no_previous_day_config.items():
        no_previous_day_shifts[ped] = {d for d in days if d.weekday() in weekdays}

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
        mir_status[ped_id] = (df[mir_col].iloc[0] == 'Yes')

        df = expand_weekday_entries(df, date_col, year, month)
        df[date_col] = pd.to_datetime(df[date_col]).dt.date

        vacation_days[ped_id] = set(df[df[reason_col] == 'Vacation'][date_col])
        congress_days[ped_id] = set(df[df[reason_col] == 'Congress'][date_col])
        skip_days[ped_id] = set(df[df[reason_col] == 'Skip'][date_col])
        prefer_not_days[ped_id] = set(df[df[pref_col] == 'Prefer Not'][date_col])
        preferred_days[ped_id] = set(df[df[pref_col] == 'Prefer'][date_col])

        cannot_work = set()
        for day in vacation_days[ped_id]:
            cannot_work.update({day, day - timedelta(days=1), day + timedelta(days=1)})
        for day in skip_days[ped_id]:
            cannot_work.add(day)
        for day in congress_days[ped_id]:
            cannot_work.update({day, day - timedelta(days=1), day + timedelta(days=1)})
        cannot_do_days[ped_id] = cannot_work

    conflicts_found = False
    for ped_id in mandatory_shifts:
        if ped_id in skip_days:
            skip_days_set = skip_days[ped_id]
            original_mandatory = mandatory_shifts[ped_id].copy()
            mandatory_shifts[ped_id] = [shift for shift in mandatory_shifts[ped_id] if shift not in skip_days_set]

            conflicts = [shift for shift in original_mandatory if shift in skip_days_set]
            if conflicts:
                conflicts_found = True
                ped_name = ped_names[ped_id]
                print(f"\n! CONFLICT RESOLUTION for {ped_name}:")
                print(f"   Skip days override mandatory shifts on: {[d.strftime('%Y-%m-%d') for d in conflicts]}")
                print(f"   Remaining mandatory shifts: {[d.strftime('%Y-%m-%d') for d in mandatory_shifts[ped_id]]}")

    if conflicts_found:
        print("\nOK All conflicts resolved - Skip days take priority over mandatory shifts")

    return {
        'days': days,
        'start_date': start_date,
        'end_date': end_date,
        'mandatory_shifts': mandatory_shifts,
        'cannot_do_days': cannot_do_days,
        'prefer_not_days': prefer_not_days,
        'preferred_days': preferred_days,
        'shift_limits': shift_limits,
        'weekend_limits': weekend_limits,
        'tipus_status': tipus_status,
        'mir_status': mir_status,
        'no_previous_day_shifts': no_previous_day_shifts
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
        'month_days': month_days,
        'overlap_days': overlap_days,
        'days_all': days_all,
        'start_date': cur['start_date'],
        'end_date': cur['end_date'],
        'mandatory_all': mandatory_all,
        'cannot_all': cannot_all,
        'prefer_not_all': prefer_not_all,
        'preferred_all': preferred_all,
        'shift_limits_cur': cur['shift_limits'],
        'weekend_limits_cur': cur['weekend_limits'],
        'shift_limits_next': (nxt['shift_limits'] if nxt else None),
        'weekend_limits_next': (nxt['weekend_limits'] if nxt else None),
        'tipus_status': cur['tipus_status'],
        'mir_status': cur['mir_status'],
        'no_previous_day_shifts': cur['no_previous_day_shifts']
    }

# -----------------------------
# Main Processing
# -----------------------------
year = 2026

base_folder = 'year26'
output_folder = base_folder
counter = 1
while os.path.exists(output_folder):
    counter += 1
    output_folder = f"{base_folder}-{counter}"
os.makedirs(output_folder)
print(f"Created folder: {output_folder}")

xls = pd.ExcelFile('year26.xlsx')
ped_sheets = [sheet for sheet in xls.sheet_names if sheet != 'MandatoryShifts']
ped_names = {i + 1: name for i, name in enumerate(ped_sheets)}
pediatricians = list(ped_names.keys())

prev_overlap_mandatory = {p: set() for p in pediatricians}

shifts_per_pediatrician_per_month = {p: {} for p in pediatricians}
weekend_shifts_per_pediatrician_per_month = {p: {} for p in pediatricians}
feasibility_data_per_month = {}
diagnostic_data_per_month = {}

# Rolling FREE trackers (exclude mandatory)
cumulative_actual_free = {p: 0.0 for p in pediatricians}
cumulative_target_free = {p: 0.0 for p in pediatricians}

for month in range(7, 13):  # July to December
    print(f"\n{'='*60}")
    print(f"PROCESSING {datetime(year, month, 1).strftime('%B %Y').upper()}")
    print(f"{'='*60}")

    last_prob = None
    last_x = None
    last_M = None
    used_M = None
    last_data = None
    last_desired_free = None
    last_mandatory_cur = None
    soft_limits_used = False

    # -----------------------------
    # Two-phase solve:
    # Phase A: hard min/max => try all M
    # Phase B: only if Phase A fails, allow soft min/max => try all M again
    # -----------------------------
    for soft_phase in [False, True]:
        if used_M is not None:
            break
        if soft_phase:
            soft_limits_used = True
            print("\n! Cap solució factible amb límits hard per a cap M. Reintentant amb límits soft...")

        for M_try in M_CANDIDATES:
            data = combine_month_with_overlap(year, month, M_try, xls, ped_sheets, ped_names, pediatricians)
            month_days = data['month_days']
            overlap_days = data['overlap_days']
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

            feasibility_data = print_feasibility_summary(shift_limits_cur, weekend_limits_cur, month_days, S1, S2)
            if M_try == M_CANDIDATES[0] and not soft_phase:
                month_name = datetime(year, month, 1).strftime("%B %Y")
                feasibility_data_per_month[month_name] = feasibility_data

            # Mandatory count in CURRENT month (incl. prev overlap fixed)
            mandatory_cur = {}
            for p in pediatricians:
                mand = set(d for d in mandatory_all.get(p, set()) if d in month_days)
                mand |= set(d for d in prev_overlap_mandatory.get(p, set()) if d in month_days)
                mandatory_cur[p] = len(mand)
            last_mandatory_cur = mandatory_cur

            # Rolling desired FREE shifts
            desired_free = {}
            for p in pediatricians:
                mn = shift_limits_cur[p]['min']
                mx = shift_limits_cur[p]['max']
                midpoint_total = 0.5 * (mn + mx)

                min_free = max(0.0, mn - mandatory_cur[p])
                max_free = max(0.0, mx - mandatory_cur[p])
                midpoint_free = max(0.0, midpoint_total - mandatory_cur[p])

                imbalance_free = cumulative_target_free[p] - cumulative_actual_free[p]
                d_free = midpoint_free + BALANCE_ALPHA * imbalance_free
                d_free = max(min_free, min(max_free, d_free))
                desired_free[p] = d_free
            last_desired_free = desired_free

            print(f"\nTargets FREE (M={M_try}, soft_limits={soft_phase}):")
            for p in pediatricians:
                print(f"  {ped_names[p]}: mandatory {mandatory_cur[p]}, desired_free {desired_free[p]:.2f}")

            # -----------------------------
            # Build model
            # -----------------------------
            prob = LpProblem(f"Pediatrician_Shift_Scheduling_{month:02d}_M{M_try}", LpMinimize)

            x = LpVariable.dicts("Shift", ((p, d) for p in pediatricians for d in days_all), cat=LpBinary)

            week_violations = LpVariable.dicts(
                "WeekViolation", ((p, w) for p in pediatricians for w in range(5)), cat=LpBinary
            )
            missed_preferred = LpVariable.dicts(
                "MissedPreferred", ((p, d) for p in pediatricians for d in days_all), cat=LpBinary
            )
            pair_violations = LpVariable.dicts(
                "PairViolation", ((p1, p2) for p1 in pediatricians for p2 in pediatricians if p1 < p2), cat=LpBinary
            )
            working_together = LpVariable.dicts(
                "WorkingTogether",
                ((p1, p2, d) for p1 in pediatricians for p2 in pediatricians for d in days_all if p1 < p2),
                cat=LpBinary
            )

            # Fairness deviation (FREE shifts)
            bal_dev_pos = LpVariable.dicts("BalDevPos", pediatricians, lowBound=0)
            bal_dev_neg = LpVariable.dicts("BalDevNeg", pediatricians, lowBound=0)

            # Only create slacks if soft_phase True
            if soft_phase:
                under_min = LpVariable.dicts("UnderMinShifts", pediatricians, lowBound=0)
                over_max = LpVariable.dicts("OverMaxShifts", pediatricians, lowBound=0)
                under_min_wknd = LpVariable.dicts("UnderMinWeekend", pediatricians, lowBound=0)
                over_max_wknd = LpVariable.dicts("OverMaxWeekend", pediatricians, lowBound=0)

            penalty_terms_base = []
            penalty_terms_fair = []

            # Prefer-not
            for p in pediatricians:
                for d in days_all:
                    if d in prefer_not_all.get(p, set()):
                        penalty_terms_base.append(PENALTY_WEIGHTS['PREFER_NOT_DAY'] * x[p, d])

            # Missed preferred
            for p in pediatricians:
                for d in days_all:
                    if d in preferred_all.get(p, set()):
                        prob += missed_preferred[p, d] >= 1 - x[p, d], f"MissPreferred_p{p}_d{d.strftime('%Y%m%d')}"
                        penalty_terms_base.append(PENALTY_WEIGHTS['MISS_PREFERRED_DAY'] * missed_preferred[p, d])

            # Excess weekly (current month only)
            for p in pediatricians:
                num_weeks = (len(month_days) + 6) // 7
                for week in range(num_weeks):
                    week_start = week * 7
                    week_end = min(week_start + 7, len(month_days))
                    week_days = month_days[week_start:week_end]
                    if week_days:
                        penalty_terms_base.append(PENALTY_WEIGHTS['EXCESS_WEEKLY_SHIFTS'] * week_violations[p, week])
                        total_week_shifts = lpSum(x[p, d] for d in week_days)
                        average_shifts = (total_week_shifts - 2) / len(week_days)
                        prob += week_violations[p, week] >= average_shifts, f"ExcessWeeklyShifts_p{p}_w{week}"

            # Repeated weekday (current month only)
            for p in pediatricians:
                for dow in range(7):
                    same_dow_days = [d for d in month_days if d.weekday() == dow]
                    if len(same_dow_days) >= 2:
                        for i in range(1, len(same_dow_days)):
                            penalty_terms_base.append(
                                PENALTY_WEIGHTS['REPEATED_WEEKDAY'] *
                                (lpSum(x[p, same_dow_days[j]] for j in range(i + 1)) - 1)
                            )

            # Repeated pairings (combined horizon)
            for p1 in pediatricians:
                for p2 in pediatricians:
                    if p1 < p2:
                        for d in days_all:
                            prob += working_together[p1, p2, d] >= x[p1, d] + x[p2, d] - 1, \
                                f"PairingMin_p{p1}_p{p2}_d{d.strftime('%Y%m%d')}"
                            prob += working_together[p1, p2, d] <= x[p1, d], \
                                f"PairingMax1_p{p1}_p{p2}_d{d.strftime('%Y%m%d')}"
                            prob += working_together[p1, p2, d] <= x[p2, d], \
                                f"PairingMax2_p{p1}_p{p2}_d{d.strftime('%Y%m%d')}"
                        prob += pair_violations[p1, p2] >= (lpSum(working_together[p1, p2, d] for d in days_all) - 1) / len(days_all), \
                            f"PairViolation_p{p1}_p{p2}"
                        penalty_terms_base.append(PENALTY_WEIGHTS['REPEATED_PAIRING'] * pair_violations[p1, p2])

            # Soft min/max penalties only in soft phase
            if soft_phase:
                for p in pediatricians:
                    penalty_terms_base.append(PENALTY_WEIGHTS['SHIFT_LIMIT_VIOLATION'] * (under_min[p] + over_max[p]))
                    penalty_terms_base.append(PENALTY_WEIGHTS['WEEKEND_LIMIT_VIOLATION'] * (under_min_wknd[p] + over_max_wknd[p]))

            # Fairness on FREE shifts only
            for p in pediatricians:
                month_total = lpSum(x[p, d] for d in month_days)
                month_free = month_total - mandatory_cur[p]
                prob += bal_dev_pos[p] >= month_free - desired_free[p], f"BalDevPosFree_p{p}"
                prob += bal_dev_neg[p] >= desired_free[p] - month_free, f"BalDevNegFree_p{p}"
                penalty_terms_fair.append(PENALTY_WEIGHTS['MONTHLY_BALANCE'] * (bal_dev_pos[p] + bal_dev_neg[p]))

            base_expr = lpSum(penalty_terms_base)
            fair_expr = lpSum(penalty_terms_fair)

            if USE_LEXICOGRAPHIC_FAIRNESS:
                prob += base_expr, "BaseObjective"
            else:
                prob += base_expr + fair_expr, "TotalObjective"

            # -----------------------------
            # Hard Constraints
            # -----------------------------
            # Mandatory shifts (current + overlap)
            for p in pediatricians:
                for d in mandatory_all.get(p, set()):
                    prob += x[p, d] == 1, f"MandatoryShift_p{p}_d{d.strftime('%Y%m%d')}"

            # Previous overlap mandatory in current month
            for p in pediatricians:
                for d in prev_overlap_mandatory.get(p, set()):
                    if d in month_days:
                        prob += x[p, d] == 1, f"PrevOverlapMandatory_p{p}_d{d.strftime('%Y%m%d')}"

            # Availability
            for p in pediatricians:
                for d in days_all:
                    if d in cannot_all.get(p, set()):
                        prob += x[p, d] == 0, f"Unavailability_p{p}_d{d.strftime('%Y%m%d')}"

            # No previous day shifts (current month only)
            for p in pediatricians:
                if p in no_previous_day_shifts:
                    for d in no_previous_day_shifts[p]:
                        prev_day = d - timedelta(days=1)
                        if prev_day in month_days:
                            prob += x[p, prev_day] == 0, f"NoPreviousDay_p{p}_d{prev_day.strftime('%Y%m%d')}"

            # Weekend limits (hard in phase A, soft in phase B)
            for p in pediatricians:
                weekend_days_cur = [d for d in month_days if d.weekday() >= 5]
                wknd_total = lpSum(x[p, d] for d in weekend_days_cur)
                if soft_phase:
                    prob += wknd_total + under_min_wknd[p] >= weekend_limits_cur[p]['min'], f"SoftMinWeekend_p{p}"
                    prob += wknd_total - over_max_wknd[p] <= weekend_limits_cur[p]['max'], f"SoftMaxWeekend_p{p}"
                else:
                    prob += wknd_total >= weekend_limits_cur[p]['min'], f"MinWeekendShifts_p{p}"
                    prob += wknd_total <= weekend_limits_cur[p]['max'], f"MaxWeekendShifts_p{p}"

            # Shift limits (hard in phase A, soft in phase B)
            for p in pediatricians:
                month_total = lpSum(x[p, d] for d in month_days)
                if soft_phase:
                    prob += month_total + under_min[p] >= shift_limits_cur[p]['min'], f"SoftMinShifts_p{p}"
                    prob += month_total - over_max[p] <= shift_limits_cur[p]['max'], f"SoftMaxShifts_p{p}"
                else:
                    prob += month_total >= shift_limits_cur[p]['min'], f"MinShifts_p{p}"
                    prob += month_total <= shift_limits_cur[p]['max'], f"MaxShifts_p{p}"

            # Soft cap on overlap (avoid dumping)
            if overlap_days and shift_limits_next and weekend_limits_next:
                for p in pediatricians:
                    prob += lpSum(x[p, d] for d in overlap_days) <= shift_limits_next[p]['max'], f"OverlapMaxShifts_p{p}"
                    overlap_weekends = [d for d in overlap_days if d.weekday() >= 5]
                    if overlap_weekends:
                        prob += lpSum(x[p, d] for d in overlap_weekends) <= weekend_limits_next[p]['max'], f"OverlapMaxWeekend_p{p}"

            # Separation across combined horizon
            for p in pediatricians:
                for d in days_all:
                    for k in range(1, M_try + 1):
                        next_day = d + timedelta(days=k)
                        if next_day in days_all:
                            prob += x[p, d] + x[p, next_day] <= 1, \
                                f"ShiftSeparation_p{p}_d{d.strftime('%Y%m%d')}_k{k}"

            # Staffing
            for d in days_all:
                prob += lpSum(x[p, d] for p in pediatricians) >= S1, f"MinStaffing_d{d.strftime('%Y%m%d')}"
                prob += lpSum(x[p, d] for p in pediatricians) <= S2, f"MaxStaffing_d{d.strftime('%Y%m%d')}"

            # Resident/supervisor rules
            residents = [p for p, t in tipus_status.items() if t == 'Resident']
            non_mir_supervisors = [p for p, can_supervise in mir_status.items() if not can_supervise]

            for d in days_all:
                for resident in residents:
                    for non_mir in non_mir_supervisors:
                        prob += x[resident, d] + x[non_mir, d] <= 1, \
                            f"NoResidentWithNonMIR_r{resident}_s{non_mir}_d{d.strftime('%Y%m%d')}"
            for d in days_all:
                prob += lpSum(x[p, d] for p in residents) <= 1, f"NoResidentPairing_d{d.strftime('%Y%m%d')}"

            # -----------------------------
            # Solve pass 1
            # -----------------------------
            prob.solve()
            last_prob, last_x, last_M, last_data = prob, x, M_try, data

            if LpStatus[prob.status] == 'Optimal':
                if USE_LEXICOGRAPHIC_FAIRNESS:
                    base_val = value(base_expr)
                    prob += base_expr <= base_val + LEXI_TOL, "FixBaseObjective"
                    prob.objective = fair_expr
                    prob.solve()

                used_M = M_try
                print(f"\nOK Solució factible amb M={used_M} (soft_limits={soft_phase})")
                break
            else:
                print(f"\n! No factible amb M={M_try} (soft_limits={soft_phase})")
                if M_try != M_CANDIDATES[-1]:
                    print(f"  -> Reintentant amb M={M_try - 1}")

    # -----------------------------
    # Diagnostics if still infeasible (rare)
    # -----------------------------
    if used_M is None:
        prob, x, data = last_prob, last_x, last_data
        days_all = data['days_all']
        month_name = datetime(year, month, 1).strftime("%B %Y")

        print(f"\n{'='*60}")
        print(f"DIAGNOSTIC ANALYSIS FOR INFEASIBILITY (last attempt M={last_M})")
        print(f"{'='*60}")

        total_required_shifts = len(days_all) * S1
        total_max_shifts = len(days_all) * S2

        conflicts = 0
        conflict_list = []
        for p in pediatricians:
            for d in data['mandatory_all'].get(p, set()) | prev_overlap_mandatory.get(p, set()):
                if d in data['cannot_all'].get(p, set()):
                    conflicts += 1
                    conflict_msg = f"{ped_names[p]} mandatory on {d.strftime('%Y-%m-%d')} but unavailable"
                    print(f"   !!! CONFLICT: {conflict_msg}")
                    conflict_list.append(conflict_msg)

        diagnostic_data_per_month[month_name] = {
            'status': LpStatus[prob.status],
            'total_days': len(days_all),
            'total_required_shifts': total_required_shifts,
            'total_max_shifts': total_max_shifts,
            'conflicts_count': conflicts,
            'conflicts_list': conflict_list,
            'last_M_attempted': last_M
        }

        prev_overlap_mandatory = {p: set() for p in pediatricians}
        continue

    # -----------------------------
    # Extract schedules + store overlap as mandatory for next month
    # -----------------------------
    data = last_data
    month_days = data['month_days']
    overlap_days = data['overlap_days']
    days_all = data['days_all']
    x = last_x
    start_date = data['start_date']
    shift_limits_cur = data['shift_limits_cur']

    schedule_month = {p: [d for d in month_days if x[p, d].varValue == 1] for p in pediatricians}

    month_name = start_date.strftime("%B %Y")
    for p in pediatricians:
        shifts_per_pediatrician_per_month[p][month_name] = len(schedule_month[p])
        weekend_shifts_per_pediatrician_per_month[p][month_name] = len([d for d in schedule_month[p] if d.weekday() >= 5])

    # Update rolling FREE trackers
    for p in pediatricians:
        assigned_total = len(schedule_month[p])
        mandatory_p = last_mandatory_cur[p] if last_mandatory_cur else 0
        assigned_free = max(0.0, assigned_total - mandatory_p)

        mn = shift_limits_cur[p]['min']
        mx = shift_limits_cur[p]['max']
        midpoint_total = 0.5 * (mn + mx)
        target_free = max(0.0, midpoint_total - mandatory_p)

        cumulative_actual_free[p] += assigned_free
        cumulative_target_free[p] += target_free

    # Overlap assignments become mandatory for next month solve
    new_prev_overlap = {p: set() for p in pediatricians}
    for p in pediatricians:
        if overlap_days:
            new_prev_overlap[p] = {d for d in overlap_days if x[p, d].varValue == 1}
    prev_overlap_mandatory = new_prev_overlap

    # -----------------------------
    # Calendar visualization (current month only)
    # -----------------------------
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(16, 12), facecolor='#1a1a1a')
    ax.set_facecolor('#1a1a1a')

    # Catalan month names
    catalan_months = {
        1: 'Gener', 2: 'Febrer', 3: 'Març', 4: 'Abril', 5: 'Maig', 6: 'Juny',
        7: 'Juliol', 8: 'Agost', 9: 'Setembre', 10: 'Octubre', 11: 'Novembre', 12: 'Desembre'
    }
    month_name_ca = catalan_months[start_date.month]
    
    ax.set_title(f'Servei de Pediatria - {month_name_ca} {start_date.year}',
                 pad=20, fontsize=18, fontweight='bold', color='#ffffff')

    num_weeks = (len(month_days) + start_date.weekday()) // 7 + 1
    ax.set_xlim(-0.5, 6.5)
    ax.set_ylim(num_weeks - 0.5, -1.0)
    ax.grid(True, linestyle='-', alpha=0.3, color='#404040')
    ax.axis('off')

    def generate_colors(num_pediatricians):
        base_colors = [
            '#FF6B6B', '#4ECDC4', '#45B7D1', '#96CEB4', '#FFEAA7',
            '#DDA0DD', '#98D8C8', '#F7DC6F', '#BB8FCE', '#85C1E9',
            '#F8C471', '#82E0AA', '#F1948A', '#85C1E9', '#D7BDE2',
            '#A9DFBF', '#F9E79F', '#D5A6BD', '#A3E4D7', '#FADBD8',
            '#D5DBDB', '#E8DAEF', '#D1F2EB', '#FCF3CF', '#EBDEF0'
        ]
        if num_pediatricians > len(base_colors):
            import colorsys
            extra = num_pediatricians - len(base_colors)
            for i in range(extra):
                hue = i / extra
                saturation = 0.3 + (i % 3) * 0.1
                value = 0.9
                rgb = colorsys.hsv_to_rgb(hue, saturation, value)
                hex_color = '#{:02x}{:02x}{:02x}'.format(
                    int(rgb[0] * 255),
                    int(rgb[1] * 255),
                    int(rgb[2] * 255)
                )
                base_colors.append(hex_color)
        return {i + 1: color for i, color in enumerate(base_colors[:num_pediatricians])}

    colors = generate_colors(len(pediatricians))

    day_labels = ['Dilluns', 'Dimarts', 'Dimecres', 'Dijous', 'Divendres', 'Dissabte', 'Diumenge']
    for day in range(7):
        ax.text(day, -0.7, day_labels[day],
                ha='center', va='center', fontsize=12,
                fontweight='bold', color='#ffffff')

    start_day = start_date.weekday()
    for num, day in enumerate(month_days):
        col = (start_day + num) % 7
        row = (start_day + num) // 7

        rect = patches.Rectangle((col - 0.48, row - 0.48), 0.96, 0.96,
                                 facecolor='#2d2d2d',
                                 edgecolor='#555555',
                                 linewidth=1.5,
                                 zorder=1)
        ax.add_patch(rect)

        ax.text(col, row - 0.3, f"{day.day}",
                ha='center', va='bottom',
                fontsize=16,
                fontweight='bold',
                color='#ffffff',
                zorder=2)

        scheduled_peds = [p for p in pediatricians if day in schedule_month[p]]
        total_peds = len(scheduled_peds)

        if total_peds > 0:
            box_height = 0.8
            spacing = box_height / total_peds
            start_y = row + 0.3

            for i, ped in enumerate(scheduled_peds):
                y_pos = start_y - (i * spacing)

                rect = patches.Rectangle((col - 0.4, y_pos - 0.15), 0.8, 0.25,
                                         facecolor=colors[ped],
                                         alpha=0.95,
                                         edgecolor='#ffffff',
                                         linewidth=1,
                                         zorder=2)
                ax.add_patch(rect)

                name = ped_names[ped]
                # Split name into first name and surname
                name_parts = name.split(maxsplit=1)
                if len(name_parts) >= 2:
                    first_name = name_parts[0]
                    surname = name_parts[1]
                    # Truncate if too long
                    if len(first_name) > 8:
                        first_name = first_name[:7] + '.'
                    if len(surname) > 8:
                        surname = surname[:7] + '.'
                else:
                    # If only one part, split it in half
                    first_name = name[:len(name)//2] if len(name) > 8 else name
                    surname = name[len(name)//2:] if len(name) > 8 else ''
                    if len(first_name) > 8:
                        first_name = first_name[:7] + '.'
                    if len(surname) > 8:
                        surname = surname[:7] + '.'
                
                # Display first name on top
                ax.text(col, y_pos - 0.05, first_name,
                        ha='center', va='center',
                        fontsize=9,
                        fontweight='bold',
                        color='#000000',
                        zorder=3)
                # Display surname on bottom
                if surname:
                    ax.text(col, y_pos + 0.05, surname,
                            ha='center', va='center',
                            fontsize=9,
                        fontweight='bold',
                        color='#000000',
                        zorder=3)

    legend_elements = [
        patches.Patch(facecolor=colors[p], alpha=0.95,
                      edgecolor='#ffffff', linewidth=1,
                      label=f'{ped_names[p]}')
        for p in pediatricians
    ]
    legend = ax.legend(handles=legend_elements,
                       loc='center left',
                       bbox_to_anchor=(1.02, 0.5),
                       title='Personal',
                       fontsize=12,
                       title_fontsize=14,
                       frameon=True,
                       fancybox=True,
                       shadow=True,
                       framealpha=0.9)

    legend.get_frame().set_facecolor('#2d2d2d')
    legend.get_frame().set_edgecolor('#555555')
    legend.get_title().set_color('#ffffff')
    for text in legend.get_texts():
        text.set_color('#ffffff')

    plt.subplots_adjust(right=0.80)

    month_name_file = start_date.strftime("%m_%Y")
    filename = f"{output_folder}/calendar_{month_name_file}_M{used_M}.png"
    plt.savefig(filename, dpi=300, bbox_inches='tight',
                facecolor='#1a1a1a', edgecolor='none')
    print(f"Calendar saved as: {filename}")

    print("\nSchedule Statistics (current month):")
    for p in pediatricians:
        total_shifts = len(schedule_month[p])
        weekend_shifts = len([d for d in schedule_month[p] if d.weekday() >= 5])
        mandatory_p = last_mandatory_cur[p] if last_mandatory_cur else 0
        free_shifts = max(0, total_shifts - mandatory_p)
        desired_f = last_desired_free[p] if last_desired_free else None

        print(f"\nPediatrician {ped_names[p]}:")
        print(f"Total shifts: {total_shifts} (Min: {shift_limits_cur[p]['min']}, Max: {shift_limits_cur[p]['max']})")
        print(f"Mandatory (current month): {mandatory_p}  | Free shifts: {free_shifts}")
        if desired_f is not None:
            print(f"Desired FREE (rolling-balance): {desired_f:.2f}")
        print(f"Weekend shifts: {weekend_shifts}")
        print(f"Assigned dates: {[d.strftime('%Y-%m-%d') for d in schedule_month[p]]}")

    if overlap_days:
        print("\nOverlap assignments fixed for next month:")
        for p in pediatricians:
            if prev_overlap_mandatory[p]:
                print(f"  {ped_names[p]}: {[d.strftime('%Y-%m-%d') for d in sorted(prev_overlap_mandatory[p])]}")

    plt.close()

# -----------------------------
# Final Summary: Detailed Statistics
# -----------------------------
print(f"\n{'='*60}")
print("FINAL SUMMARY: DETAILED STATISTICS")
print(f"{'='*60}\n")

all_months = set()
for p in pediatricians:
    all_months.update(shifts_per_pediatrician_per_month[p].keys())
all_months.update(feasibility_data_per_month.keys())

def month_sort_key(month_str):
    try:
        month_date = datetime.strptime(month_str, "%B %Y")
        return (month_date.year, month_date.month)
    except:
        return (0, 0)

all_months = sorted(all_months, key=month_sort_key)

summary_lines = []
summary_lines.append("="*120)
summary_lines.append(f"{'Pediatrician':<20} | " + " | ".join([f"{m[:3]:^5}" for m in all_months]) + " | Total | Wknd | Wkday | Wkday(h) | Wknd(h) | Total(h)")
summary_lines.append("-" * 120)

grand_total_shifts = 0

for p in pediatricians:
    row_str = f"{ped_names[p]:<20} | "
    total_p = 0
    total_weekend_p = 0
    
    for month in all_months:
        shifts = shifts_per_pediatrician_per_month[p].get(month, 0)
        wknd = weekend_shifts_per_pediatrician_per_month[p].get(month, 0)
        
        row_str += f"{shifts:^5} | "
        total_p += shifts
        total_weekend_p += wknd
    
    total_weekday_p = total_p - total_weekend_p
    weekday_hours = total_weekday_p * 15
    weekend_hours = total_weekend_p * 24
    total_hours = weekday_hours + weekend_hours
    
    grand_total_shifts += total_p
    
    row_str += f"{total_p:^5} | {total_weekend_p:^4} | {total_weekday_p:^5} | {weekday_hours:^8} | {weekend_hours:^7} | {total_hours:^8}"
    summary_lines.append(row_str)

summary_lines.append("-" * 120)

# Print to console
for line in summary_lines:
    print(line)

summary_filename = f"{output_folder}/shifts_summary.txt"
with open(summary_filename, 'w', encoding='utf-8') as f:
    f.write("\n".join(summary_lines))

print(f"\nSummary report saved to: {summary_filename}")
