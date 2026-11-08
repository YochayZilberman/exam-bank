"""Stage 5 -- auto-crop questions from exam pages.

    data/questions/<exam_id>/q<n>_<part>.png

Every auto-crop is stored with crop_status='unverified'. If header detection fails, the exam is
marked 'needs_manual' and no crops are produced. Exams marked 'verified' or 'manual' are never
touched, and neither are individual questions you've verified or drawn by hand.

Incremental: only exams with crop_status='pending' are processed; --force re-crops 'auto' and
'needs_manual' exams too (still keeping verified/manual questions).
"""
from __future__ import annotations

import json

from .common import PAGES_DIR, QUESTIONS_DIR, Config, abs_path, get_logger, rel
from .db import connect
from .layout import crop_questions, detect_headers

log = get_logger("crop")


def drop_auto_crops(con, exam_id: str) -> None:
    """Delete an exam's unverified auto crops (files + rows). Verified/manual ones stay."""
    for r in con.execute("SELECT id, images FROM questions WHERE exam_id=? AND crop_status='unverified'",
                         (exam_id,)).fetchall():
        for im in json.loads(r["images"]):
            p = abs_path(im["path"])
            if p.exists():
                p.unlink()
        con.execute("DELETE FROM question_tags WHERE question_id=? AND state='suggested'", (r["id"],))
        con.execute("DELETE FROM questions WHERE id=?", (r["id"],))


def page_images(item_id: str, page_count: int):
    imgs = [PAGES_DIR / item_id / f"page_{i}.png" for i in range(1, page_count + 1)]
    missing = [p for p in imgs if not p.exists()]
    return imgs, missing


def run(cfg: Config, course: str | None = None, force: bool = False, **_):
    con = connect()
    codes = [c.code for c in cfg.select(course)]
    states = ("pending", "auto", "needs_manual") if force else ("pending",)
    rows = con.execute(
        f"SELECT * FROM exams WHERE course IN ({','.join('?' * len(codes))}) "
        f"AND crop_status IN ({','.join('?' * len(states))}) ORDER BY id", codes + list(states)
    ).fetchall()
    ok = failed = 0
    for ex in rows:
        imgs, missing = page_images(ex["id"], ex["page_count"] or 0)
        if not imgs or missing:
            log.warning("%s: pages not rendered yet, run the render stage first", ex["id"])
            continue
        try:
            headers, note, last_page, margins = detect_headers(abs_path(ex["pdf_path"]), imgs, ex["id"])
        except Exception as e:
            headers, note, last_page, margins = [], f"header detection error: {e}", len(imgs), {}

        if not headers:
            drop_auto_crops(con, ex["id"])  # stale auto crops from an earlier run would be wrong
            con.execute("UPDATE exams SET crop_status='needs_manual', crop_note=? WHERE id=?",
                        (note, ex["id"]))
            con.commit()
            log.info("%s: needs manual cropping -- %s", ex["id"], note)
            failed += 1
            continue

        keep = {r["number"] for r in con.execute(
            "SELECT number FROM questions WHERE exam_id=? AND crop_status IN ('verified','manual')",
            (ex["id"],))}
        drop_auto_crops(con, ex["id"])  # keeps verified / manual questions

        # Crop with ALL headers as boundaries (so a kept manual question still ends the one
        # before it), then drop the ones you've already verified/drawn.
        crops = crop_questions(headers, imgs[:last_page], QUESTIONS_DIR / ex["id"], skip=keep,
                               margins=margins)
        for num, parts in crops.items():
            if not parts:
                continue
            images = [{"page": p["page"], "path": rel(p["path"]), "bbox": p["bbox"]} for p in parts]
            con.execute(
                "INSERT INTO questions(id,exam_id,number,images,first_page,crop_status) "
                "VALUES (?,?,?,?,?,'unverified')",
                (f"{ex['id']}_q{num}", ex["id"], num, json.dumps(images), parts[0]["page"]),
            )
        con.execute("UPDATE exams SET crop_status='auto', crop_note=? WHERE id=?", (note, ex["id"]))
        con.commit()
        log.info("%s: %d questions (%s)", ex["id"], len(crops), note)
        ok += 1
    log.info("cropped %d exams, %d need manual cropping", ok, failed)
