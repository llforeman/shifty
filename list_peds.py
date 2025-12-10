from app import app, db, Pediatrician

with app.app_context():
    peds = Pediatrician.query.all()
    print(f"Total pediatricians: {len(peds)}")
    for p in peds:
        print(f"ID: {p.id}, Name: {p.name}, Service ID: {p.service_id}")
