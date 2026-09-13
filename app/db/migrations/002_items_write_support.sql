-- Support for issue 8's write endpoints: hiding a derived item without
-- deleting it, and marking one item as today's focus.

ALTER TABLE items ADD COLUMN dismissed_at TEXT;

CREATE TABLE focus (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    item_id    INTEGER REFERENCES items(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL
);
