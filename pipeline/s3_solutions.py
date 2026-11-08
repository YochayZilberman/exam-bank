"""Stage 3 -- collect solutions.

The Aguda academic bank (aguda.org.il/academic-bank) requires a student login (checked
2026-11-06: the page redirects to the home page and the tile shows a lock). Login is not
automated, so solutions come only from the manual folder:

    data/manual/<course>/solutions/**.pdf

Metadata is parsed from file/folder names (same rules as exams). Each solution is matched to
an exam by course + year + semester + moed. If the solution's semester is unknown but
course + year + moed identify exactly one exam, that exam is used. Anything else is left
'unmatched' for the Solution matcher in the site. Manual matches are never changed.
"""
from __future__ import annotations

import shutil

import pymupdf as fitz

from .common import (MANUAL_DIR, SOLUTIONS_DIR, UNKNOWN, Config, get_logger,
                     parse_metadata_from_path, rel, sha256_file)
from .db import connect, sync_courses

log = get_logger("solutions")


def find_exam(con, course: str, year: str, sem: str, moed: str) -> str | None:
    if UNKNOWN in (year, moed):
        return None
    if sem != UNKNOWN:
        rows = con.execute("SELECT id FROM exams WHERE course=? AND year=? AND semester=? AND moed=? "
                           "ORDER BY version", (course, year, sem, moed)).fetchall()
    else:
        rows = con.execute("SELECT id FROM exams WHERE course=? AND year=? AND moed=? ORDER BY version",
                           (course, year, moed)).fetchall()
        if len({r["id"].rsplit("_v", 1)[0] for r in rows}) != 1:
            return None
    return rows[0]["id"] if rows else None


def run(cfg: Config, course: str | None = None, **_):
    con = connect()
    sync_courses(con, cfg)
    for c in cfg.select(course):
        base = MANUAL_DIR / c.code / "solutions"
        base.mkdir(parents=True, exist_ok=True)
        new = 0
        for p in sorted(x for x in base.rglob("*") if x.suffix.lower() == ".pdf"):
            orig = rel(p)
            if con.execute("SELECT 1 FROM solutions WHERE original_path=?", (orig,)).fetchone():
                continue
            sha = sha256_file(p)
            if con.execute("SELECT 1 FROM solutions WHERE sha256=?", (sha,)).fetchone():
                log.info("  skip %s (identical file already imported)", orig)
                continue
            m = parse_metadata_from_path(p, base)
            sub = p.relative_to(base).parts
            source = sub[0].lower() if len(sub) > 1 and sub[0].lower() in ("aguda", "drive") else "manual"
            sid = f"sol_{c.code}_{m['year']}_{m['semester']}_{m['moed']}_{sha[:8]}"
            dest = SOLUTIONS_DIR / c.code / f"{sid}.pdf"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, dest)
            with fitz.open(dest) as doc:
                pages = doc.page_count
            con.execute("INSERT INTO solutions(id,course,year,semester,moed,source,original_path,"
                        "pdf_path,sha256,page_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (sid, c.code, m["year"], m["semester"], m["moed"], source, orig,
                         rel(dest), sha, pages))
            new += 1

        # (Re)match everything that isn't manually matched -- new exams may have arrived.
        matched = unmatched = 0
        for s in con.execute("SELECT * FROM solutions WHERE course=? AND match_status!='manual'",
                             (c.code,)).fetchall():
            eid = find_exam(con, c.code, s["year"], s["semester"], s["moed"])
            con.execute("UPDATE solutions SET exam_id=?, match_status=? WHERE id=?",
                        (eid, "auto" if eid else "unmatched", s["id"]))
            matched += bool(eid)
            unmatched += not eid
        con.commit()
        log.info("%s: %d new solution files; %d matched, %d unmatched", c.code, new, matched, unmatched)
