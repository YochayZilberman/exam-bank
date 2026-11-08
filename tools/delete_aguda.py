"""Remove every Aguda solution from the bank (user request, 2026-11-07).

Deletes the solution rows, their copied PDFs, rendered pages and solution crops, and the files in
data/manual/*/solutions/aguda/. Questions of affected exams lose their solution crops; the other
solutions of those exams are set back to 'pending' so `run.py crop-solutions` re-links them.
Exams, questions, tags and personal progress are not touched.
"""
import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
con = sqlite3.connect(ROOT / "data/bank.db")
con.row_factory = sqlite3.Row

rows = con.execute("SELECT id, exam_id, pdf_path FROM solutions WHERE source='aguda'").fetchall()
exams = {r["exam_id"] for r in rows if r["exam_id"]}
for r in rows:
    (ROOT / r["pdf_path"]).unlink(missing_ok=True)
    shutil.rmtree(ROOT / "data/pages" / r["id"], ignore_errors=True)
    shutil.rmtree(ROOT / "data/solution_crops" / r["id"], ignore_errors=True)
con.execute("DELETE FROM solutions WHERE source='aguda'")
for eid in exams:
    con.execute("UPDATE questions SET solution_images=NULL, solution_page=NULL WHERE exam_id=?", (eid,))
    con.execute("UPDATE solutions SET crop_status='pending' WHERE exam_id=?", (eid,))
con.commit()

files = list((ROOT / "data/manual").glob("*/solutions/aguda"))
n = sum(len(list(d.glob("*.pdf"))) for d in files)
for d in files:
    shutil.rmtree(d)
print(f"deleted {len(rows)} solution rows and {n} files; {len(exams)} exams to re-link")
