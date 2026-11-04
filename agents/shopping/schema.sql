-- Applied on every start; must be safe to run again (IF NOT EXISTS).
CREATE TABLE IF NOT EXISTS shopping_items (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    name       TEXT NOT NULL,              -- as shown to the user: "zeytinyağı"
    name_key   TEXT NOT NULL,              -- normalized for matching: lowercase, single spaces
    quantity   INTEGER,                    -- NULL = "some", no number given
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | bought | removed
    created_at TEXT NOT NULL,
    done_at    TEXT                        -- when bought or removed
);
CREATE INDEX IF NOT EXISTS shopping_items_by_user ON shopping_items (user_id, status);
