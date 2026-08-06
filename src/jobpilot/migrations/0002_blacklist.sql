-- Employers the owner has decided not to apply to again. Matched at capture,
-- before any tailoring work is spent on the posting.
CREATE TABLE blacklist(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'company',
    pattern TEXT NOT NULL,
    normalised TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);

CREATE UNIQUE INDEX blacklist_kind_normalised ON blacklist(kind, normalised);
