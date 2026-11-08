"""One-off cleanup after the first Drive import (2026-11-07).

* Drive "exams" that are CamScanner scans turned out to be handwritten student solutions:
  move them from exams/drive/ to solutions/drive/.
* Files whose name says the exam comes with solutions (עם פתרונות) also go to solutions.
* Drop every Drive exam row (and its copied PDF/pages) so `run.py exams --skip-huji` re-imports
  the Drive folder with the stricter duplicate rule (one exam per course/year/moed).
"""
import shutil
import sqlite3
from pathlib import Path

import pymupdf as fitz

ROOT = Path(__file__).resolve().parents[1]
con = sqlite3.connect(ROOT / "data/bank.db")
con.row_factory = sqlite3.Row

moved = 0
for p in sorted((ROOT / "data/manual").glob("*/exams/drive/*.pdf")):
    with fitz.open(p) as d:
        text = " ".join(d[i].get_text() for i in range(min(2, d.page_count)))
    if "CamScanner" in text or "עם פתרונות" in p.name:
        dest = p.parents[2] / "solutions" / "drive" / p.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), dest)
        moved += 1
        print("-> solutions:", p.parent.parent.parent.name, p.name)

rows = con.execute("SELECT id, pdf_path FROM exams WHERE source='drive'").fetchall()
for r in rows:
    eid = r["id"]
    if con.execute("SELECT 1 FROM questions WHERE exam_id=? AND crop_status IN ('verified','manual')",
                   (eid,)).fetchone():
        print("keeping (has your crops):", eid)
        continue
    con.execute("DELETE FROM question_tags WHERE question_id IN (SELECT id FROM questions WHERE exam_id=?)", (eid,))
    con.execute("DELETE FROM questions WHERE exam_id=?", (eid,))
    con.execute("UPDATE solutions SET exam_id=NULL, match_status='unmatched' "
                "WHERE exam_id=? AND match_status!='manual'", (eid,))
    con.execute("DELETE FROM exams WHERE id=?", (eid,))
    (ROOT / r["pdf_path"]).unlink(missing_ok=True)
    shutil.rmtree(ROOT / "data/pages" / eid, ignore_errors=True)
con.execute("DELETE FROM duplicates WHERE location LIKE 'data/manual/%/exams/drive/%'")
con.commit()
print(f"moved {moved} files to solutions; dropped {len(rows)} drive exam rows for re-import")
