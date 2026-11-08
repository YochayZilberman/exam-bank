"""Stage 1 -- syllabus topics from the Shnaton (shnaton.huji.ac.il).

The Shnaton is a single-page app backed by a public JSON API:
    GET /api/syllabus?courseCode=<code>&year=<year>
We take `courseContent` (he/en) and split it into topics.

Output: data/topics/<course>.json. Existing files are NOT overwritten (you may have edited
them) unless --force is given. The raw API response is cached in data/cache/shnaton/.
"""
from __future__ import annotations

import re
from datetime import datetime

import requests

from .common import CACHE_DIR, TOPICS_DIR, Config, RateLimiter, get_logger, read_json, write_json

API = "https://shnaton.huji.ac.il/api/syllabus"
UA = "huji-exam-bank/0.1 (personal study tool)"
log = get_logger("topics")
limiter = RateLimiter(2.0)


def fetch_syllabus(code: str, year: int, refresh: bool) -> dict | None:
    cache = CACHE_DIR / "shnaton" / f"{code}_{year}.json"
    if cache.exists() and not refresh:
        return read_json(cache)
    limiter.wait()
    r = requests.get(API, params={"courseCode": code, "year": year},
                     headers={"User-Agent": UA}, timeout=30)
    if r.status_code != 200 or not r.text.strip().startswith("{"):
        log.warning("%s: syllabus request returned %s", code, r.status_code)
        return None
    data = r.json()
    write_json(cache, data)
    return data


_BULLET = re.compile(r"^\s*(?:[-*•·●▪]|\(?\d{1,2}[.)]|\(?[א-ת][.)]|[a-z][.)])\s*", re.I)


def split_topics(text: str | None) -> list[str]:
    if not text:
        return []
    text = text.replace("\r", "")
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    # In a numbered/bulleted list, a line without a marker continues the previous item
    # (wrapped text) -- unless the previous line ends a heading like "Probability Theory:".
    if sum(bool(_BULLET.match(l)) for l in lines) >= 3:
        merged: list[str] = []
        for l in lines:
            if merged and not _BULLET.match(l) and not merged[-1].rstrip().endswith(":"):
                merged[-1] += " " + l
            else:
                merged.append(l)
        lines = merged
    # Lines that look like headers ("Probability Theory:") are kept as topics too.
    if len(lines) <= 2:
        # One paragraph: split on ';' first, then on sentence ends.
        joined = " ".join(lines)
        pieces = re.split(r";|\.\s+(?=[A-Zא-ת])", joined)
        lines = [p.strip() for p in pieces if p.strip()]
    out = []
    for l in lines:
        l = _BULLET.sub("", l).strip(" .;:-–")
        l = re.sub(r"\s+", " ", l)
        if 2 < len(l) <= 160:
            out.append(l)
    return out


def build_topics(code: str, data: dict) -> list[dict]:
    content = data.get("courseContent") or {}
    he = split_topics(content.get("he"))
    en = split_topics(content.get("en"))
    topics = []
    if he and en and len(he) == len(en):
        topics = [{"he": h, "en": e} for h, e in zip(he, en)]
    else:
        # Counts differ: keep both lists without pairing rather than mis-pair them.
        topics = [{"he": h, "en": None} for h in he] + [{"he": None, "en": e} for e in en]
    for i, t in enumerate(topics, 1):
        t["id"] = f"t{i:02d}"
    return topics


def run(cfg: Config, course: str | None = None, force: bool = False, refresh: bool = False, **_):
    for c in cfg.select(course):
        out = TOPICS_DIR / f"{c.code}.json"
        if out.exists() and not force:
            log.info("%s: topics file exists, keeping it (use --force to regenerate)", c.code)
            continue
        data = None
        for code in c.all_codes:
            data = fetch_syllabus(code, cfg.shnaton_year, refresh)
            cc = (data or {}).get("courseContent") or {}
            if cc.get("he") or cc.get("en"):
                break
        if not data:
            log.warning("%s: no syllabus found in the Shnaton", c.code)
            continue
        topics = build_topics(c.code, data)
        write_json(out, {
            "course": c.code,
            "source": f"{API}?courseCode={c.code}&year={cfg.shnaton_year}",
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "note": "Edit freely. Each topic needs a unique 'id'; tags reference these ids. "
                    "This file is not overwritten on re-runs unless you pass --force.",
            "topics": topics,
            "raw": data.get("courseContent"),
        })
        log.info("%s: wrote %d topics -> %s", c.code, len(topics), out.name)
