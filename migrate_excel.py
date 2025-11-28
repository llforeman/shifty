import pandas as pd
from datetime import datetime, timedelta
from app import app, db, Pediatrician, Preference, seed_global_config

# --- HELPER FUNCTIONS (Reused from pediweb.py) ---
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
        if name in df.columns:
            return name
    # Fallback or error handling if needed, but for migration we assume structure is known
    return None

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
        return []
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
    if expanded_rows:
        result_df = pd.DataFrame(expanded_rows)
        # Ensure columns match original
        result_df = result_df.reindex(columns=df.columns)
        return result_df
    else:
        return pd.DataFrame(columns=df.columns)

def migrate_data():
    excel_file = 'year26.xlsx'
    # We'll process months 7 to 12 as per the user's original script logic, 
    # OR we can just process all sheets. The prompt says "Por cada hoja de cálculo (ped_sheets)".
    # The original script iterates months 7-12 but reads the SAME sheets. 
    # The sheets contain the limits (fixed) and preferences (dates).
    # The preferences might be spread across months if the excel has date entries for multiple months?
    # Actually, looking at pediweb.py, it reads the sheet ONCE per month loop, but the sheet itself contains ALL preferences?
    # No, `process_month` reads the sheet and filters? 
    # Wait, `process_month` calls `expand_weekday_entries` with a specific year/month.
    # If the Excel contains "Mondays", it implies "Mondays of THIS month". 
    # So we DO need to iterate through the months to generate the specific dates if they are defined as "Monday".
    # However, if they are specific dates (e.g. 2026-07-15), they are just dates.
    # Let's assume we need to cover the relevant period. The user's script covers months 7-12 of 2026.
    
    year = 2026
    months = range(7, 13) # July to Dec

    print("Reading Excel file...")
    xls = pd.ExcelFile(excel_file)
    ped_sheets = [sheet for sheet in xls.sheet_names if sheet != 'MandatoryShifts']
    
    with app.app_context():
        # 1. Reset Database
        print("Dropping and recreating tables...")
        db.drop_all()
        db.create_all()
        seed_global_config()
        
        # 2. Process Pediatricians (Static Data)
        # We can read the first sheet to get the list of peds, or iterate all.
        # The limits are at the top of each sheet.
        
        ped_map = {} # Name -> ID
        
        for i, sheet_name in enumerate(ped_sheets):
            print(f"Processing sheet: {sheet_name}")
            df = pd.read_excel(xls, sheet_name=sheet_name)
            
            # Extract Limits & Metadata
            number_col = get_column_name(df, 'number')
            weekend_col = get_column_name(df, 'weekend')
            type_col = get_column_name(df, 'type')
            mir_col = get_column_name(df, 'mir')
            
            min_shifts = int(df[number_col].iloc[0])
            max_shifts = int(df[number_col].iloc[1])
            min_weekend = int(df[weekend_col].iloc[0])
            max_weekend = int(df[weekend_col].iloc[1])
            
            p_type = df[type_col].iloc[0]
            is_mir = (str(df[mir_col].iloc[0]).strip().lower() == 'yes')
            
            # Create Pediatrician
            ped = Pediatrician(
                name=sheet_name,
                min_shifts=min_shifts,
                max_shifts=max_shifts,
                min_weekend=min_weekend,
                max_weekend=max_weekend,
                type=p_type,
                mir=is_mir
            )
            db.session.add(ped)
            db.session.flush() # Get ID
            ped_map[sheet_name] = ped.id
            
            # 3. Process Preferences
            # We need to handle both explicit dates and "Weekday" entries for each month.
            # We will iterate through the months 7-12 to expand any weekday entries.
            # Explicit dates will be handled naturally if we just read them, but `expand_weekday_entries` 
            # is designed to take a specific month context.
            
            # To avoid duplicates if "Monday" generates dates for July, August, etc., 
            # we should probably iterate months and collect all unique preferences.
            
            all_prefs = [] # List of (date, type) tuples
            
            date_col = get_column_name(df, 'date')
            reason_col = get_column_name(df, 'reason')
            pref_col = get_column_name(df, 'preference')
            
            # We need to be careful. If the Excel has a row "Monday" | "Vacation", 
            # does it mean every Monday of the year? Or just the months we process?
            # The original script `process_month` is called for each month.
            # It reads the sheet afresh.
            # So "Monday" means "Monday of the current processing month".
            # So we MUST iterate months 7-12 and generate preferences for each.
            
            for m in months:
                # Re-read or copy df to avoid mutating the original for next iteration?
                # expand_weekday_entries mutates or returns new? It returns new.
                # But we need to pass the original df rows.
                
                # Actually, `expand_weekday_entries` iterates rows.
                # If a row is a date, it keeps it.
                # If a row is "Monday", it expands it to dates in that month.
                # If we run this for July, "Monday" -> July Mondays.
                # If we run for August, "Monday" -> August Mondays.
                # But what about the fixed dates? "2026-07-15". 
                # If we run for August, "2026-07-15" is still "2026-07-15".
                # We don't want to add "2026-07-15" 6 times (once per month loop).
                # But `Preference` table has a unique constraint on (ped_id, date).
                # So we can just try to add and ignore duplicates, or use a set.
                
                expanded = expand_weekday_entries(df, date_col, year, m)
                
                # Now extract preferences from this expanded df
                # Types: Vacation, Skip, Prefer Not, Prefer
                # Mapped from 'reason' (Vacation, Skip, Congress) and 'preference' (Prefer, Prefer Not)
                
                for _, row in expanded.iterrows():
                    d = row[date_col]
                    if not isinstance(d, (datetime, type(pd.NaT))):
                         # It might be a string if conversion failed, but expand handles it.
                         # It returns datetime.date objects or NaT
                         pass
                    
                    # Check Reason column
                    reason = row[reason_col]
                    if pd.notna(reason):
                        r_str = str(reason).strip()
                        if r_str == 'Vacation':
                            all_prefs.append((d, 'Vacation'))
                        elif r_str == 'Skip':
                            all_prefs.append((d, 'Skip'))
                        elif r_str == 'Congress':
                            # Map Congress to Vacation or Skip? 
                            # User prompt says: "Vacation, Skip, Prefer, etc."
                            # In `pediweb.py`, Congress is treated similar to Vacation (cannot work).
                            # Let's map it to 'Vacation' (or add a new type if we want to be specific, but 'Vacation' is safe for "Cannot Do")
                            # Actually, let's stick to the types in `app.py` prompt: 'Vacation', 'Skip', 'Prefer Not', 'Prefer'
                            # I'll map Congress to 'Vacation' for now as it blocks the day.
                            all_prefs.append((d, 'Vacation')) 
                            
                    # Check Preference column
                    pref = row[pref_col]
                    if pd.notna(pref):
                        p_str = str(pref).strip()
                        if p_str == 'Prefer':
                            all_prefs.append((d, 'Prefer'))
                        elif p_str == 'Prefer Not':
                            all_prefs.append((d, 'Prefer Not'))

            # Deduplicate and Insert
            # Use a dictionary to keep unique dates. 
            # If conflict (e.g. Vacation and Prefer on same day?), Vacation should win.
            # Priority: Vacation/Skip > Prefer Not/Prefer
            
            unique_prefs = {}
            for d, p_type in all_prefs:
                if not isinstance(d, (datetime, pd.Timestamp)) and not hasattr(d, 'strftime'):
                     # Skip invalid dates
                     continue
                # Ensure it's a python date
                if isinstance(d, pd.Timestamp):
                    d = d.date()
                
                if d in unique_prefs:
                    # Conflict resolution logic if needed
                    # If existing is Prefer/Prefer Not and new is Vacation/Skip, overwrite.
                    current = unique_prefs[d]
                    if current in ['Prefer', 'Prefer Not'] and p_type in ['Vacation', 'Skip']:
                        unique_prefs[d] = p_type
                else:
                    unique_prefs[d] = p_type
            
            for d, p_type in unique_prefs.items():
                # Check if it's already in session (from previous month loop iteration for fixed dates)
                # But we are using a dict `unique_prefs` for this pediatrician, so no dupes here.
                # We just need to make sure we don't violate DB constraint if we run this script multiple times (but we drop_all at start).
                
                pref_entry = Preference(pediatrician_id=ped.id, date=d, type=p_type)
                db.session.add(pref_entry)

        db.session.commit()
        print("Migration completed successfully.")

if __name__ == '__main__':
    migrate_data()
