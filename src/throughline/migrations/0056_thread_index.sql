-- Purging a channel deletes its threads; each deleted thread checks
-- tl.messages.thread_id, and the existing thread index only covers current
-- rows. Without a full index every deleted thread scans the whole table
-- (found purging a live channel).
CREATE INDEX IF NOT EXISTS messages_thread_id_all ON tl.messages(thread_id);
