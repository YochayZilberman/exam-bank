"""Run the pipeline.

    python run.py                       # all stages, all courses
    python run.py all --course 83911    # all stages, one course
    python run.py exams render crop     # selected stages
    python run.py status                # summary of what's in the database

Options:
    --course CODE   only this course (alt codes like 80430 work too)
    --force         redo work that's normally skipped (regenerate topics, re-crop auto crops,
                    re-tag). Manual fixes and personal fields are still never overwritten.
    --refresh       ignore cached web responses (Shnaton / HUJI search results)
    --skip-huji     don't open the browser for the HUJI Test Library (manual folder only)
"""
from __future__ import annotations

import argparse
import sys

from pipeline import (s1_topics, s2_exams, s3_solutions, s4_render, s5_crop, s6_crop_solutions,
                      s7_tag, s8_build)
from pipeline.common import load_config

STAGES = {
    "topics": s1_topics.run,
    "exams": s2_exams.run,
    "solutions": s3_solutions.run,
    "render": s4_render.run,
    "crop": s5_crop.run,
    "crop-solutions": s6_crop_solutions.run,
    "tag": s7_tag.run,
    "build": s8_build.run,
}


def status() -> None:
    from pipeline.db import connect

    con = connect()
    print(f"{'course':8} {'exams':>5} {'needs crop':>10} {'questions':>9} {'unverified':>10} "
          f"{'tagged':>6} {'solutions':>9} {'unmatched':>9} {'unknown meta':>12}")
    for r in con.execute("""
        SELECT c.code,
          (SELECT COUNT(*) FROM exams e WHERE e.course=c.code) exams,
          (SELECT COUNT(*) FROM exams e WHERE e.course=c.code AND e.crop_status='needs_manual') nm,
          (SELECT COUNT(*) FROM questions q JOIN exams e ON e.id=q.exam_id WHERE e.course=c.code) qs,
          (SELECT COUNT(*) FROM questions q JOIN exams e ON e.id=q.exam_id WHERE e.course=c.code
              AND q.crop_status='unverified') unv,
          (SELECT COUNT(DISTINCT t.question_id) FROM question_tags t JOIN questions q ON q.id=t.question_id
              JOIN exams e ON e.id=q.exam_id WHERE e.course=c.code) tagged,
          (SELECT COUNT(*) FROM solutions s WHERE s.course=c.code) sols,
          (SELECT COUNT(*) FROM solutions s WHERE s.course=c.code AND s.match_status='unmatched') um,
          (SELECT COUNT(*) FROM exams e WHERE e.course=c.code AND 'unknown' IN (e.year,e.semester,e.moed)) unk
        FROM courses c ORDER BY c.code"""):
        print(f"{r[0]:8} {r[1]:>5} {r[2]:>10} {r[3]:>9} {r[4]:>10} {r[5]:>6} {r[6]:>9} {r[7]:>9} {r[8]:>12}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stages", nargs="*", default=["all"],
                    help="all | status | " + " | ".join(STAGES))
    ap.add_argument("--course")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--skip-huji", action="store_true")
    a = ap.parse_args(argv)

    if a.stages == ["status"]:
        status()
        return 0
    stages = list(STAGES) if "all" in a.stages else a.stages
    bad = [s for s in stages if s not in STAGES]
    if bad:
        ap.error(f"unknown stage(s): {', '.join(bad)}")
    cfg = load_config()
    for s in stages:
        print(f"\n=== {s} ===", flush=True)
        STAGES[s](cfg, course=a.course, force=a.force, refresh=a.refresh, skip_huji=a.skip_huji)
    return 0


if __name__ == "__main__":
    sys.exit(main())
