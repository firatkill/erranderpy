-- Applied on every start; must be safe to run again (IF NOT EXISTS).
-- Prefix tables with the agent name: agents never share tables.
CREATE TABLE IF NOT EXISTS {{name}}_entries (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS {{name}}_entries_by_user ON {{name}}_entries (user_id);
