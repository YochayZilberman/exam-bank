"""Stage 8 -- build the queryable question table.

Everything already lives in SQLite (data/bank.db); this stage (re)creates:
  * view  question_rows -- one row per question with exam metadata, tags, solution link and
                           personal fields joined in (what the site reads);
  * table question_fts  -- FTS5 full-text index over summary, question text, tags, course names.
  * a `progress` row for every question (status 'not tried') without touching existing ones.
It never modifies personal fields or manual fixes.
"""
from __future__ import annotations

import json

from .common import TOPICS_DIR, Config, get_logger, read_json
from .db import connect

log = get_logger("build")

VIEW = """
DROP VIEW IF EXISTS question_rows;
CREATE VIEW question_rows AS
SELECT q.id               AS question_id,
       e.course           AS course,
       c.name_en          AS course_name_en,
       c.name_he          AS course_name_he,
       e.year, e.semester, e.moed, e.lecturer, e.source,
       q.number           AS question_number,
       q.images, q.first_page AS exam_page,
       e.id               AS exam_id,
       e.pdf_path         AS exam_pdf,
       q.crop_status, q.summary, q.question_text,
       (SELECT s.pdf_path FROM solutions s WHERE s.exam_id = e.id
         ORDER BY s.match_status = 'manual' DESC, s.id LIMIT 1) AS solution_pdf,
       (SELECT json_group_array(json_object('path', x.pdf_path, 'source', x.source, 'name', x.original_path))
          FROM (SELECT s.pdf_path, s.source, s.original_path FROM solutions s WHERE s.exam_id = e.id
                ORDER BY s.match_status = 'manual' DESC, s.id) x) AS solution_pdfs,
       q.solution_images, q.solution_page,
       (SELECT json_group_array(json_object('topic', t.topic_id, 'state', t.state))
          FROM question_tags t WHERE t.question_id = q.id) AS tags,
       COALESCE(p.status, 'not tried') AS status,
       p.difficulty, p.notes
FROM questions q
JOIN exams e   ON e.id = q.exam_id
JOIN courses c ON c.code = e.course
LEFT JOIN progress p  ON p.question_id = q.id;
"""


def run(cfg: Config, **_):
    con = connect()
    con.executescript(VIEW)
    con.execute("INSERT OR IGNORE INTO progress(question_id) SELECT id FROM questions")
    # Placeholder rows for questions that no longer exist (re-cropped away) are removed only if
    # untouched; anything you edited is kept even if its question is gone.
    con.execute("DELETE FROM progress WHERE question_id NOT IN (SELECT id FROM questions) "
                "AND status='not tried' AND difficulty IS NULL AND (notes IS NULL OR notes='')")

    topic_names = {}
    for c in cfg.courses:
        tf = read_json(TOPICS_DIR / f"{c.code}.json") or {}
        for t in tf.get("topics", []):
            topic_names[(c.code, t["id"])] = " ".join(x for x in (t.get("en"), t.get("he")) if x)

    con.executescript("""
        DROP TABLE IF EXISTS question_fts;
        CREATE VIRTUAL TABLE question_fts USING fts5(
            question_id UNINDEXED, course, summary, question_text, topics,
            tokenize = 'unicode61 remove_diacritics 2');
    """)
    rows = con.execute("SELECT * FROM question_rows").fetchall()
    for r in rows:
        tags = json.loads(r["tags"] or "[]")
        topics = " ".join(topic_names.get((r["course"], t["topic"]), t["topic"]) for t in tags)
        con.execute("INSERT INTO question_fts VALUES (?,?,?,?,?)",
                    (r["question_id"], f"{r['course']} {r['course_name_en']} {r['course_name_he']}",
                     r["summary"] or "", r["question_text"] or "", topics))
    con.commit()

    stats = con.execute("""
        SELECT e.course, COUNT(DISTINCT e.id) exams, COUNT(q.id) questions
        FROM exams e LEFT JOIN questions q ON q.exam_id=e.id GROUP BY e.course""").fetchall()
    for s in stats:
        log.info("%s: %d exams, %d questions", s["course"], s["exams"], s["questions"])
    log.info("database built: %d question rows", len(rows))
