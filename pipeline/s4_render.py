"""Stage 4 -- render every exam and solution page to PNG (~200 DPI).

    data/pages/<exam_id>/page_<n>.png        (n starts at 1)
    data/pages/<solution_id>/page_<n>.png

Pages that already exist are skipped.
"""
from __future__ import annotations

import pymupdf as fitz

from .common import PAGES_DIR, Config, abs_path, get_logger
from .db import connect

DPI = 200
log = get_logger("render")


def render_pdf(pdf_rel: str, out_id: str) -> int:
    out_dir = PAGES_DIR / out_id
    out_dir.mkdir(parents=True, exist_ok=True)
    made = 0
    with fitz.open(abs_path(pdf_rel)) as doc:
        for i, page in enumerate(doc, 1):
            target = out_dir / f"page_{i}.png"
            if target.exists():
                continue
            pix = page.get_pixmap(dpi=DPI, colorspace=fitz.csGRAY)
            pix.save(target)
            made += 1
    return made


def run(cfg: Config, course: str | None = None, **_):
    con = connect()
    codes = [c.code for c in cfg.select(course)]
    q = ",".join("?" * len(codes))
    total = 0
    for row in con.execute(f"SELECT id, pdf_path FROM exams WHERE course IN ({q}) "
                           f"UNION ALL SELECT id, pdf_path FROM solutions WHERE course IN ({q})",
                           codes + codes).fetchall():
        try:
            n = render_pdf(row["pdf_path"], row["id"])
        except Exception as e:  # a broken PDF shouldn't stop the run
            log.error("%s: render failed: %s", row["id"], e)
            continue
        if n:
            log.info("%s: rendered %d pages", row["id"], n)
        total += n
    log.info("rendered %d new pages", total)
