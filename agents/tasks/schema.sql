-- Applied on every start; must be safe to run again (IF NOT EXISTS).

-- A habit is a rule: "every day, ingilizce okuma, 30 minutes".
CREATE TABLE IF NOT EXISTS habits (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL,
    name          TEXT NOT NULL,
    name_key      TEXT NOT NULL,          -- normalized for matching
    daily_minutes INTEGER,                -- NULL = no time target, just "did it or not"
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    ended_at      TEXT
);
CREATE INDEX IF NOT EXISTS habits_by_user ON habits (user_id, active);

-- A task is one thing for one day. Habits produce one task per day
-- (habit_id set); one-off tasks have habit_id NULL.
CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL,
    day            TEXT NOT NULL,         -- the user's local date, YYYY-MM-DD
    title          TEXT NOT NULL,
    title_key      TEXT NOT NULL,
    habit_id       INTEGER REFERENCES habits (id),
    target_minutes INTEGER,
    done_minutes   INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'open',   -- open | done
    created_at     TEXT NOT NULL,
    done_at        TEXT
);
CREATE INDEX IF NOT EXISTS tasks_by_user_day ON tasks (user_id, day);

-- Idempotency: at most one task per habit per day. Creating the day's habit
-- tasks twice (the midnight job AND the first message of the day) can't duplicate them.
CREATE UNIQUE INDEX IF NOT EXISTS tasks_one_per_habit_day ON tasks (habit_id, day) WHERE habit_id IS NOT NULL;
