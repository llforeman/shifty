from app import app, db, User, Pediatrician
import unicodedata

def normalize_name(name):
    # Normalize to ascii (remove accents)
    nfkd_form = unicodedata.normalize('NFKD', name)
    only_ascii = nfkd_form.encode('ASCII', 'ignore').decode('utf-8')
    return only_ascii.lower().strip()

with app.app_context():
    print("🚀 Starting bulk user creation from Pediatricians...")
    
    peds = Pediatrician.query.all()
    created_count = 0
    skipped_count = 0
    
    for ped in peds:
        # Check if user already linked
        existing_user = User.query.filter_by(pediatrician_id=ped.id).first()
        if existing_user:
            print(f"⏩ User already exists for '{ped.name}', skipping.")
            skipped_count += 1
            continue
            
        # Generate Email
        # "Name Surname" -> "name.surname@chv.cat"
        normalized = normalize_name(ped.name)
        # Replace spaces with dots
        email_local = normalized.replace(' ', '.')
        email = f"{email_local}@chv.cat"
        
        # Check if email is taken (unlikely but possible)
        if User.query.filter_by(email=email).first():
            print(f"⚠️  Email {email} already taken! Appending id.")
            email = f"{email_local}.{ped.id}@chv.cat"
            
        # Create User
        # Using email as username as per previous decision
        new_user = User(
            username=email,
            email=email,
            role='user', # Default role
            pediatrician_id=ped.id,
            must_change_password=True
        )
        new_user.set_password('1111')
        
        db.session.add(new_user)
        print(f"✅ Created user: {email} for '{ped.name}'")
        created_count += 1
        
    db.session.commit()
    print(f"\n🎉 Finished! Created: {created_count}, Skipped: {skipped_count}")
