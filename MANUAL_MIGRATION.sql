-- Run this SQL on your production database to fix the chat error:

ALTER TABLE chat_message ADD COLUMN recipient_id INTEGER;

-- Note: This adds the column without a foreign key constraint
-- The FK would be: FOREIGN KEY (recipient_id) REFERENCES user(id)
-- but for MySQL ALTER TABLE, adding it separately might cause issues
-- so we're skipping the constraint for now
