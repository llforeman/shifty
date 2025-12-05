from app import app, db, Shift

with app.app_context():
    count = Shift.query.count()
    print(f"Total shifts in database: {count}")
    
    if count > 0:
        first = Shift.query.order_by(Shift.date).first()
        last = Shift.query.order_by(Shift.date.desc()).first()
        print(f"First shift: {first.date}")
        print(f"Last shift: {last.date}")
