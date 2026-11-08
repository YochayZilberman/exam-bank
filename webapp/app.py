"""Local web app for the question bank (FastAPI). Run with:  python serve.py

Serves the single-page UI from webapp/static and a small JSON API over data/bank.db.
Personal fields (progress) and manual fixes are written only here; the pipeline never
overwrites them.
"""
from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

from pipeline.common import DATA, PAGES_DIR, QUESTIONS_DIR, TOPICS_DIR, abs_path, load_config, read_json, rel
from pipeline.db import connect, sync_courses
from pipeline.s8_build import VIEW

STATIC = Path(__file__).parent / "static"
STATUSES = ["not tried", "tried", "solved", "review again"]

app = FastAPI(title="Exam bank", docs_url="/api/docs")


@app.middleware("http")
async def no_cache_ui(request, call_next):
    """The UI files change when the project is updated; don't let the browser keep stale ones."""
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def db() -> sqlite3.Connection:
    con = connect()
    return con


@app.on_event("startup")
def _startup() -> None:
    con = db()
    sync_courses(con, load_config())
    con.executescript(VIEW)   # make sure the view exists even if `build` never ran
    con.execute("INSERT OR IGNORE INTO progress(question_id) SELECT id FROM questions")
    con.commit()


def topics_for(course: str) -> list[dict]:
    return (read_json(TOPICS_DIR / f"{course}.json") or {}).get("topics", [])


# ------------------------------------------------------------------ read API

@app.get("/api/meta")
def meta():
    con = db()
    cfg = load_config()
    courses = []
    for c in cfg.courses:
        stats = con.execute("""
            SELECT COUNT(DISTINCT e.id) exams, COUNT(q.id) questions,
                   SUM(COALESCE(p.status,'not tried')='solved') solved
            FROM exams e LEFT JOIN questions q ON q.exam_id=e.id
            LEFT JOIN progress p ON p.question_id=q.id WHERE e.course=?""", (c.code,)).fetchone()
        courses.append({"code": c.code, "name_en": c.name_en, "name_he": c.name_he,
                        "alt_codes": c.alt_codes, "topics": topics_for(c.code), **dict(stats)})
    queue = con.execute("""SELECT
        (SELECT COUNT(*) FROM exams WHERE crop_status='needs_manual') needs_manual,
        (SELECT COUNT(*) FROM questions WHERE crop_status='unverified') unverified,
        (SELECT COUNT(*) FROM exams WHERE 'unknown' IN (year, semester, moed)) unknown_meta,
        (SELECT COUNT(*) FROM solutions WHERE match_status='unmatched') unmatched,
        (SELECT COUNT(DISTINCT question_id) FROM question_tags t WHERE state='suggested' AND NOT EXISTS
            (SELECT 1 FROM question_tags c WHERE c.question_id=t.question_id AND c.state='confirmed')) suggested
    """).fetchone()
    return {"courses": courses, "statuses": STATUSES, "queue": dict(queue)}


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["images"] = json.loads(d["images"] or "[]")
    d["solution_images"] = json.loads(d["solution_images"]) if d.get("solution_images") else []
    d["tags"] = [t for t in json.loads(d["tags"] or "[]") if t and t.get("topic")]
    d["solution_pdfs"] = [s for s in json.loads(d.get("solution_pdfs") or "[]") if s and s.get("path")]
    return d


@app.get("/api/questions")
def questions():
    """All question rows; filtering/sorting happens in the browser (a few thousand at most)."""
    con = db()
    rows = con.execute("SELECT * FROM question_rows ORDER BY course, year DESC, semester, moed, question_number")
    return [_row(r) for r in rows]


@app.get("/api/questions/{qid}")
def question(qid: str):
    r = db().execute("SELECT * FROM question_rows WHERE question_id=?", (qid,)).fetchone()
    if not r:
        raise HTTPException(404)
    return _row(r)


@app.get("/api/search")
def search(q: str):
    """Full-text search (FTS5 index built by the pipeline's build stage) -> question ids."""
    con = db()
    try:
        rows = con.execute("SELECT question_id FROM question_fts WHERE question_fts MATCH ? LIMIT 500",
                           (" ".join(f'"{w}"*' for w in q.replace('"', " ").split()),)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r[0] for r in rows]


@app.get("/api/practice")
def practice(course: str = "", topics: str = "", n: int = 5, include_solved: bool = False):
    con = db()
    rows = [_row(r) for r in con.execute("SELECT * FROM question_rows")]
    want = set(filter(None, topics.split(",")))
    pool = [r for r in rows
            if (not course or r["course"] == course)
            and (include_solved or r["status"] != "solved")
            and (not want or want & {t["topic"] for t in r["tags"]})]
    random.shuffle(pool)
    return pool[:max(1, min(n, 50))]


# ------------------------------------------------------------------ personal fields

class Progress(BaseModel):
    status: str | None = None
    difficulty: int | None = None
    notes: str | None = None


@app.patch("/api/progress/{qid}")
def set_progress(qid: str, body: Progress):
    con = db()
    if not con.execute("SELECT 1 FROM questions WHERE id=?", (qid,)).fetchone():
        raise HTTPException(404)
    con.execute("INSERT OR IGNORE INTO progress(question_id) VALUES (?)", (qid,))
    fields = body.model_dump(exclude_unset=True)
    if "status" in fields and fields["status"] not in STATUSES:
        raise HTTPException(400, "bad status")
    if "difficulty" in fields and fields["difficulty"] is not None and not 1 <= fields["difficulty"] <= 5:
        raise HTTPException(400, "difficulty must be 1-5")
    for k, v in fields.items():
        con.execute(f"UPDATE progress SET {k}=?, updated_at=CURRENT_TIMESTAMP WHERE question_id=?", (v, qid))
    con.commit()
    return question(qid)


# ------------------------------------------------------------------ tags

class Tags(BaseModel):
    topics: list[str]


@app.put("/api/questions/{qid}/tags")
def set_tags(qid: str, body: Tags):
    """Replace the question's tags with these, all confirmed."""
    con = db()
    if not con.execute("SELECT 1 FROM questions WHERE id=?", (qid,)).fetchone():
        raise HTTPException(404)
    con.execute("DELETE FROM question_tags WHERE question_id=?", (qid,))
    con.executemany("INSERT INTO question_tags VALUES (?,?,'confirmed')", [(qid, t) for t in dict.fromkeys(body.topics)])
    con.commit()
    return question(qid)


# ------------------------------------------------------------------ exams / cropping

@app.get("/api/exams")
def exams():
    con = db()
    rows = con.execute("""
        SELECT e.*, (SELECT COUNT(*) FROM questions q WHERE q.exam_id=e.id) n_questions,
               (SELECT COUNT(*) FROM questions q WHERE q.exam_id=e.id AND q.crop_status='unverified') n_unverified
        FROM exams e ORDER BY e.course, e.year DESC, e.semester, e.moed, e.id""")
    return [dict(r) for r in rows]


@app.get("/api/exams/{eid}")
def exam(eid: str):
    con = db()
    e = con.execute("SELECT * FROM exams WHERE id=?", (eid,)).fetchone()
    if not e:
        raise HTTPException(404)
    pages = sorted((PAGES_DIR / eid).glob("page_*.png"), key=lambda p: int(p.stem.split("_")[1]))
    qs = [{"id": r["id"], "number": r["number"], "crop_status": r["crop_status"],
           "images": json.loads(r["images"])}
          for r in con.execute("SELECT * FROM questions WHERE exam_id=? ORDER BY number", (eid,))]
    return {**dict(e), "pages": [rel(p) for p in pages], "questions": qs}


class ExamMeta(BaseModel):
    year: str | None = None
    semester: str | None = None
    moed: str | None = None
    lecturer: str | None = None


@app.patch("/api/exams/{eid}")
def set_exam_meta(eid: str, body: ExamMeta):
    """Fix metadata by hand. The exam id stays the same; meta_locked stops the pipeline
    from touching these fields again."""
    con = db()
    if not con.execute("SELECT 1 FROM exams WHERE id=?", (eid,)).fetchone():
        raise HTTPException(404)
    f = body.model_dump(exclude_unset=True)
    for k in ("semester", "moed"):
        if k in f and f[k] not in ("A", "B", "C", "unknown"):
            raise HTTPException(400, f"{k} must be A, B, C or unknown")
    if "year" in f and not (f["year"] == "unknown" or (f["year"].isdigit() and len(f["year"]) == 4)):
        raise HTTPException(400, "year must be YYYY or unknown")
    for k, v in f.items():
        con.execute(f"UPDATE exams SET {k}=?, meta_locked=1 WHERE id=?", (v, eid))
    con.commit()
    return exam(eid)


class Part(BaseModel):
    page: int
    bbox: list[float]   # [x0,y0,x1,y1] in page-image pixels


class ManualQuestion(BaseModel):
    number: int
    parts: list[Part]


def _delete_question(con, qid: str) -> None:
    r = con.execute("SELECT images FROM questions WHERE id=?", (qid,)).fetchone()
    if r:
        for im in json.loads(r["images"]):
            p = abs_path(im["path"])
            if p.exists():
                p.unlink()
    con.execute("DELETE FROM questions WHERE id=?", (qid,))
    con.execute("DELETE FROM question_tags WHERE question_id=? AND state='suggested'", (qid,))


@app.post("/api/exams/{eid}/questions")
def save_manual_question(eid: str, body: ManualQuestion):
    """Create or replace question <number> of this exam from boxes drawn on page images."""
    con = db()
    if not con.execute("SELECT 1 FROM exams WHERE id=?", (eid,)).fetchone():
        raise HTTPException(404)
    if body.number < 1 or not body.parts:
        raise HTTPException(400, "need a question number >= 1 and at least one box")
    qid = f"{eid}_q{body.number}"
    _delete_question(con, qid)
    out_dir = QUESTIONS_DIR / eid
    out_dir.mkdir(parents=True, exist_ok=True)
    images = []
    for i, part in enumerate(body.parts, 1):
        page_img = PAGES_DIR / eid / f"page_{part.page}.png"
        if not page_img.exists():
            raise HTTPException(400, f"no page {part.page}")
        im = Image.open(page_img)
        x0, y0, x1, y1 = part.bbox
        box = (max(0, int(min(x0, x1))), max(0, int(min(y0, y1))),
               min(im.width, int(max(x0, x1))), min(im.height, int(max(y0, y1))))
        if box[2] - box[0] < 10 or box[3] - box[1] < 10:
            raise HTTPException(400, "box too small")
        path = out_dir / f"q{body.number}_{i}.png"
        im.crop(box).save(path)
        images.append({"page": part.page, "path": rel(path), "bbox": list(box)})
    con.execute("INSERT INTO questions(id,exam_id,number,images,first_page,crop_status) VALUES (?,?,?,?,?,'manual')",
                (qid, eid, body.number, json.dumps(images), body.parts[0].page))
    con.execute("INSERT OR IGNORE INTO progress(question_id) VALUES (?)", (qid,))
    con.commit()
    return exam(eid)


@app.delete("/api/questions/{qid}")
def delete_question(qid: str):
    con = db()
    r = con.execute("SELECT exam_id FROM questions WHERE id=?", (qid,)).fetchone()
    if not r:
        raise HTTPException(404)
    _delete_question(con, qid)
    con.commit()
    return exam(r["exam_id"])


class Status(BaseModel):
    status: str


@app.post("/api/questions/{qid}/crop_status")
def set_question_crop_status(qid: str, body: Status):
    if body.status not in ("unverified", "verified", "manual"):
        raise HTTPException(400)
    con = db()
    con.execute("UPDATE questions SET crop_status=? WHERE id=?", (body.status, qid))
    con.commit()
    return question(qid)


@app.post("/api/exams/{eid}/done")
def exam_done(eid: str):
    """Mark an exam's cropping as finished: auto crops become verified and the exam is
    locked against re-cropping."""
    con = db()
    if not con.execute("SELECT 1 FROM exams WHERE id=?", (eid,)).fetchone():
        raise HTTPException(404)
    con.execute("UPDATE questions SET crop_status='verified' WHERE exam_id=? AND crop_status='unverified'", (eid,))
    manual = con.execute("SELECT 1 FROM questions WHERE exam_id=? AND crop_status='manual'", (eid,)).fetchone()
    con.execute("UPDATE exams SET crop_status=? WHERE id=?", ("manual" if manual else "verified", eid))
    con.commit()
    return exam(eid)


# ------------------------------------------------------------------ solutions

@app.get("/api/solutions")
def solutions():
    con = db()
    return [dict(r) for r in con.execute("SELECT * FROM solutions ORDER BY course, match_status DESC, year")]


class Match(BaseModel):
    exam_id: str | None


@app.patch("/api/solutions/{sid}")
def match_solution(sid: str, body: Match):
    con = db()
    if body.exam_id and not con.execute("SELECT 1 FROM exams WHERE id=?", (body.exam_id,)).fetchone():
        raise HTTPException(400, "unknown exam")
    con.execute("UPDATE solutions SET exam_id=?, match_status=? WHERE id=?",
                (body.exam_id, "manual" if body.exam_id else "unmatched", sid))
    con.commit()
    return dict(con.execute("SELECT * FROM solutions WHERE id=?", (sid,)).fetchone())


# ------------------------------------------------------------------ files + UI

@app.get("/files/{path:path}")
def files(path: str):
    """Serve PDFs and images from data/ only."""
    p = (DATA / path).resolve()
    if DATA.resolve() not in p.parents or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
