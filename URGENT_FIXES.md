# URGENT FIXES NEEDED

## Issue 1: Chat Database Error
**Problem**: The `recipient_id` column doesn't exist in production database.

**Solution**: Run this SQL on your Render database:
```sql
ALTER TABLE chat_message ADD COLUMN recipient_id INTEGER;
```

**How to run it**:
1. Go to Render dashboard → Your database
2. Click "Connect" → Use External Connection or web shell
3. Run the SQL command above

## Issue 2: Calendar Swap Not Working
**Problem**: Only YOUR shift card is draggable. Other users' cards don't ALL have `data-shift-id`, so they can't be detected as drop targets.

**Root Cause**: Line 141-143 in calendar.html - the `data-shift-id` attribute is only added to shifts that are draggable (manager's shifts or your own). Other users' shifts don't have this attribute, so the drop detection on line 217 (`targetElement.dataset.shiftId`) returns `undefined`.

**Solution**: ALL shift cards need `data-shift-id`, not just draggable ones. The `draggable` attribute should remain conditional, but `data-shift-id` needs to be on every shift.

I will push the calendar fix now.
