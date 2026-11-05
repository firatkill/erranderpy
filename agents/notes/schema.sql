-- Applied on every start; must be safe to run again (IF NOT EXISTS).
CREATE TABLE IF NOT EXISTS notes_entries (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    title      TEXT NOT NULL DEFAULT '',   -- short; listings show only this (older DBs: tools.migrate)
    text       TEXT NOT NULL,
    tags       TEXT NOT NULL DEFAULT '',   -- space-separated, lowercase: "kitap fikir"
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS notes_entries_by_user ON notes_entries (user_id);
