"""SQLite schema and connection.

Ownership rules (so re-runs never clobber manual work):
  * `progress`            -- personal fields; written ONLY by the site.
  * exams.meta_locked=1   -- metadata fixed by hand; stage 2 won't touch year/semester/moed.
  * questions.crop_status in ('verified','manual') -- stage 5 never re-crops or deletes them,
    and exams.crop_status in ('verified','manual') are skipped entirely.
  * question_tags.state='confirmed' -- stage 7 never replaces confirmed tags.
  * solutions.match_status='manual' -- stage 3 never re-matches.
"""
from __future__ import annotations

import sqlite3

from .common import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    code      TEXT PRIMARY KEY,
    name_en   TEXT,
    name_he   TEXT,
    alt_codes TEXT              -- JSON list
);

CREATE TABLE IF NOT EXISTS exams (
    id            TEXT PRIMARY KEY,   -- e.g. 83911_2024_A_B  (course_year_semester_moed[_vN])
    course        TEXT NOT NULL,      -- canonical course code
    source_course TEXT,               -- the number it was found under (e.g. 80430)
    year          TEXT NOT NULL,
    semester      TEXT NOT NULL,      -- A / B / C / unknown
    moed          TEXT NOT NULL,      -- A / B / C / unknown
    version       INTEGER DEFAULT 1,
    lecturer      TEXT DEFAULT 'unknown',
    source        TEXT NOT NULL,      -- hujitest / drive / manual
    source_url    TEXT,               -- original URL (hujitest)
    original_path TEXT,               -- original file (manual folder)
    pdf_path      TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    page_count    INTEGER,
    crop_status   TEXT DEFAULT 'pending',  -- pending / auto / needs_manual / verified / manual
    crop_note     TEXT,
    meta_locked   INTEGER DEFAULT 0,
    added_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS exams_sha ON exams(sha256);

CREATE TABLE IF NOT EXISTS duplicates (   -- files skipped by dedup, for review
    sha256        TEXT,
    kept_exam_id  TEXT,
    source        TEXT,
    location      TEXT,
    reason        TEXT,
    PRIMARY KEY (sha256, location)
);

CREATE TABLE IF NOT EXISTS solutions (
    id            TEXT PRIMARY KEY,
    course        TEXT NOT NULL,
    year          TEXT NOT NULL,
    semester      TEXT NOT NULL,
    moed          TEXT NOT NULL,
    source        TEXT NOT NULL,
    original_path TEXT,
    pdf_path      TEXT NOT NULL,
    sha256        TEXT NOT NULL UNIQUE,
    page_count    INTEGER,
    exam_id       TEXT REFERENCES exams(id),
    match_status  TEXT DEFAULT 'unmatched',  -- auto / manual / unmatched
    crop_status   TEXT DEFAULT 'pending',    -- pending / auto / failed
    added_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS questions (
    id            TEXT PRIMARY KEY,   -- <exam_id>_q<n>
    exam_id       TEXT NOT NULL REFERENCES exams(id),
    number        INTEGER NOT NULL,
    images        TEXT NOT NULL,      -- JSON [{page, path, bbox:[x0,y0,x1,y1] in page px}]
    first_page    INTEGER,
    crop_status   TEXT DEFAULT 'unverified',  -- unverified / verified / manual
    summary       TEXT,
    question_text TEXT,
    solution_images TEXT,             -- JSON list like images (best effort)
    solution_page INTEGER,            -- fallback: page in the solution PDF
    updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS questions_exam ON questions(exam_id);

CREATE TABLE IF NOT EXISTS question_tags (
    question_id TEXT NOT NULL,
    topic_id    TEXT NOT NULL,
    state       TEXT NOT NULL,        -- suggested / confirmed
    PRIMARY KEY (question_id, topic_id)
);

CREATE TABLE IF NOT EXISTS progress (    -- personal fields, owned by the site
    question_id TEXT PRIMARY KEY,
    status      TEXT DEFAULT 'not tried', -- not tried / tried / solved / review again
    difficulty  INTEGER,                  -- 1..5
    notes       TEXT,
    updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def sync_courses(con: sqlite3.Connection, cfg) -> None:
    import json

    for c in cfg.courses:
        con.execute(
            "INSERT INTO courses(code,name_en,name_he,alt_codes) VALUES (?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET name_en=excluded.name_en, name_he=excluded.name_he, "
            "alt_codes=excluded.alt_codes",
            (c.code, c.name_en, c.name_he, json.dumps(c.alt_codes)),
        )
    con.commit()
