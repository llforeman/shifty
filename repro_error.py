from app import app, db, Shift

with app.app_context():
    shift = Shift.query.first()
    if shift:
        print(f"Shift ID: {shift.id}")
        try:
            print(f"Pediatrician Name: {shift.pediatrician.name}")
        except AttributeError as e:
            print(f"Caught expected error: {e}")
        except Exception as e:
            print(f"Caught unexpected error: {e}")
    else:
        print("No shifts found to test.")
