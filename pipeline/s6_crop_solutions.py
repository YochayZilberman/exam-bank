"""Stage 6 -- best-effort crop of matched solutions, linked to each question.

If solution headers are found, each question gets solution_images (crops in
data/solution_crops/<solution_id>/s<n>_<part>.png). If not, each question gets
solution_page: the page where its number seems to start, or page 1 -- so the site can open the
full solution PDF at that page.
"""
from __future__ import annotations

import json

from .common import SOLUTION_CROPS_DIR, Config, abs_path, get_logger, rel
from .db import connect
from .layout import crop_questions, detect_headers
from .s5_crop import page_images

log = get_logger("crop-solutions")


def run(cfg: Config, course: str | None = None, force: bool = False, **_):
    con = connect()
    codes = [c.code for c in cfg.select(course)]
    states = ("pending", "auto", "failed") if force else ("pending",)
    sols = con.execute(
        f"SELECT * FROM solutions WHERE exam_id IS NOT NULL AND course IN ({','.join('?' * len(codes))}) "
        f"AND crop_status IN ({','.join('?' * len(states))})", codes + list(states)).fetchall()
    for s in sols:
        imgs, missing = page_images(s["id"], s["page_count"] or 0)
        if missing:
            log.warning("%s: pages not rendered yet", s["id"])
            continue
        try:
            headers, note, last_page, margins = detect_headers(abs_path(s["pdf_path"]), imgs, s["id"])
        except Exception as e:
            headers, note, last_page, margins = [], str(e), len(imgs), {}
        qs = con.execute("SELECT id, number FROM questions WHERE exam_id=?", (s["exam_id"],)).fetchall()
        if headers:
            crops = crop_questions(headers, imgs[:last_page], SOLUTION_CROPS_DIR / s["id"], prefix="s",
                                   margins=margins)
            for q in qs:
                parts = crops.get(q["number"]) or []
                images = [{"page": p["page"], "path": rel(p["path"]), "bbox": p["bbox"]} for p in parts]
                first = parts[0]["page"] if parts else 1
                con.execute("UPDATE questions SET solution_images=?, solution_page=? WHERE id=?",
                            (json.dumps(images) if images else None, first, q["id"]))
            status = "auto"
        else:
            for q in qs:
                con.execute("UPDATE questions SET solution_images=NULL, solution_page=1 WHERE id=?", (q["id"],))
            status = "failed"
        con.execute("UPDATE solutions SET crop_status=? WHERE id=?", (status, s["id"]))
        con.commit()
        log.info("%s -> %s: %s (%s)", s["id"], s["exam_id"], status, note)
