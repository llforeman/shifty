from app import app, db, Pediatrician, User
import pandas as pd

with app.app_context():
    # Read the Excel file to find Montse Ruiz
    xls = pd.ExcelFile('year26.xlsx')
    ped_sheets = [sheet for sheet in xls.sheet_names if sheet != 'MandatoryShifts']
    
    print("Pediatricians in Excel file:")
    for i, name in enumerate(ped_sheets, 1):
        print(f"  ID: {i}, Name: {name}")
        
        # Check if this pediatrician exists in database
        ped = Pediatrician.query.filter_by(id=i).first()
        if not ped:
            print(f"    -> Not in database yet")
        else:
            print(f"    -> In database: {ped.name}")
            
            # Check if user exists for this pediatrician
            user = User.query.filter_by(pediatrician_id=i).first()
            if user:
                print(f"    -> User exists: {user.username}")
            else:
                print(f"    -> No user account yet")
    
    # Find Montse Ruiz specifically
    print("\n" + "="*50)
    montse_id = None
    montse_name = None
    for i, name in enumerate(ped_sheets, 1):
        if "Montse" in name or "MONTSE" in name.upper():
            montse_id = i
            montse_name = name
            print(f"\nFound Montse Ruiz: ID={montse_id}, Name={montse_name}")
            
            # Check if user exists
            user = User.query.filter_by(pediatrician_id=montse_id).first()
            if user:
                print(f"User account already exists: {user.username}")
                print(f"\nTo access:")
                print(f"  Username: {user.username}")
                print(f"  (Password was set when account was created)")
            else:
                print("\nNo user account exists yet.")
                print("Creating user account for Montse Ruiz...")
                
                # Create username from name (lowercase, no spaces)
                username = montse_name.lower().replace(" ", "_").replace(".", "")
                
                # Check if username already exists
                if User.query.filter_by(username=username).first():
                    username = f"{username}_{montse_id}"
                
                # Create user
                new_user = User(
                    username=username,
                    role='user',
                    pediatrician_id=montse_id
                )
                new_user.set_password('shifty2026')  # Default password
                db.session.add(new_user)
                db.session.commit()
                
                print(f"\n✅ User created successfully!")
                print(f"\nLogin credentials:")
                print(f"  Username: {username}")
                print(f"  Password: shifty2026")
                print(f"\nMontse can change the password after first login in the Profile section.")
            break
    
    if not montse_id:
        print("\n⚠️  Could not find Montse Ruiz in the Excel file.")
        print("Please check the sheet names in year26.xlsx")
