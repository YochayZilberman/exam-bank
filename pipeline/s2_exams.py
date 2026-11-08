"""Stage 2 -- collect exam PDFs.

Sources
  1. HUJI Test Library (www4.huji.ac.il/htbin/exams/exams.cgi).
     The site sits behind a firewall that rejects plain scripted HTTP clients, so this stage
     opens a *visible* Microsoft Edge window with Playwright and makes the requests from inside
     that page, slowly (one request every few seconds). Nothing is done to hide the automation.
     Search results are cached in data/cache/huji/<code>.json for 7 days.
  2. Manual folder: data/manual/<course>/exams/**.pdf  (files under a folder named "drive"
     are recorded with source=drive, everything else source=manual).

Dedup: an identical file (same SHA-256) is never imported twice. A different file whose
course/year/semester/moed match an exam we already have from another source is skipped and
listed in the `duplicates` table so you can review it.
"""
from __future__ import annotations

import base64
import json
import shutil
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import pymupdf as fitz

from .common import (CACHE_DIR, EXAMS_DIR, MANUAL_DIR, MOEDS, SEMESTERS, UNKNOWN, Config,
                     RateLimiter, exam_id_for, get_logger, parse_metadata_from_path, read_json,
                     rel, sha256_file, write_json)
from .db import connect, sync_courses

FORM_URL = "https://www4.huji.ac.il/htbin/exams/exams.cgi"
CACHE_TTL = timedelta(days=7)
REQUEST_INTERVAL = 4.0  # seconds between requests to the university server
log = get_logger("exams")

# Runs inside the page: POST the search form and return the result rows.
SEARCH_JS = """
async (code) => {
  const body = new URLSearchParams({action:'mode2', coursenum:code, coursename:'', teachername:'',
      keywords:'', year2:String(new Date().getFullYear()), year1:'1990', moed:'0', semester:'0',
      Submit:'שלח'});
  const r = await fetch('/htbin/exams/exams.cgi', {method:'POST', body,
      headers:{'Content-Type':'application/x-www-form-urlencoded'}});
  const html = new TextDecoder('windows-1255').decode(await r.arrayBuffer());
  const d = new DOMParser().parseFromString(html, 'text/html');
  const rows = [...d.querySelectorAll('tr')]
     .filter(tr => !tr.querySelector('tr') && tr.querySelector('a[href$=".pdf"]'))
     .map(tr => ({cells: [...tr.cells].map(td => td.innerText.trim()),
                  url: tr.querySelector('a[href$=".pdf"]').href}));
  return {status: r.status, rows, noResults: html.includes('אין תוצאות'),
          blocked: html.includes('Something went wrong')};
}
"""

# Runs inside the page: download one PDF and return it base64-encoded.
FETCH_PDF_JS = """
async (url) => {
  const r = await fetch(url, {credentials: 'include'});
  const buf = new Uint8Array(await r.arrayBuffer());
  let s = ''; const CH = 0x8000;
  for (let i = 0; i < buf.length; i += CH) s += String.fromCharCode.apply(null, buf.subarray(i, i + CH));
  return {status: r.status, type: r.headers.get('content-type'), b64: btoa(s)};
}
"""


# ------------------------------------------------------------------ import helpers

def _page_count(pdf: Path) -> int | None:
    try:
        with fitz.open(pdf) as doc:
            return doc.page_count
    except Exception:
        return None


def import_pdf(con, tmp_pdf: Path, *, course: str, source_course: str, meta: dict,
               source: str, source_url: str | None, original_path: str | None) -> str | None:
    """Dedup + copy into data/exams + insert row. Returns exam id, or None if skipped."""
    sha = sha256_file(tmp_pdf)
    location = source_url or original_path
    hit = con.execute("SELECT id FROM exams WHERE sha256=?", (sha,)).fetchone()
    if hit:
        if location and not con.execute(
                "SELECT 1 FROM exams WHERE id=? AND (source_url=? OR original_path=?)",
                (hit["id"], location, location)).fetchone():
            con.execute("INSERT OR IGNORE INTO duplicates VALUES (?,?,?,?,?)",
                        (sha, hit["id"], source, location, "identical file"))
        return None

    year, sem, moed, ver = meta["year"], meta["semester"], meta["moed"], meta.get("version", 1)
    eid = exam_id_for(course, year, sem, moed, ver)
    if sem == UNKNOWN and UNKNOWN not in (year, moed):
        # Only the semester is missing (typical for Drive file names). If another source
        # already has this course/year/moed, treat it as the same exam rather than adding a
        # second copy; it's listed in `duplicates` so it can be checked.
        twin = con.execute("SELECT id FROM exams WHERE course=? AND year=? AND moed=? "
                           "ORDER BY source='hujitest' DESC, id LIMIT 1", (course, year, moed)).fetchone()
        if twin:
            con.execute("INSERT OR IGNORE INTO duplicates VALUES (?,?,?,?,?)",
                        (sha, twin["id"], source, location, "same course/year/moed, semester unknown"))
            log.info("  probable duplicate of %s: %s", twin["id"], location)
            return None
    if UNKNOWN in (year, sem, moed):
        eid = f"{eid}_{sha[:8]}"  # keep unknown-metadata exams distinct
    existing = con.execute("SELECT id, source FROM exams WHERE id=?", (eid,)).fetchone()
    if existing:
        if existing["source"] != source:
            con.execute("INSERT OR IGNORE INTO duplicates VALUES (?,?,?,?,?)",
                        (sha, eid, source, location, "same course/year/semester/moed, different file"))
            log.info("  duplicate by metadata, kept %s (from %s)", eid, existing["source"])
            return None
        # Same source, same metadata, different file: store as a new version.
        n = 2
        while con.execute("SELECT 1 FROM exams WHERE id=?", (f"{eid}_v{n}",)).fetchone():
            n += 1
        eid, ver = f"{eid}_v{n}", n

    dest = EXAMS_DIR / course / (eid[len(course) + 1:] + ".pdf")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(tmp_pdf, dest)
    con.execute(
        "INSERT INTO exams(id,course,source_course,year,semester,moed,version,source,source_url,"
        "original_path,pdf_path,sha256,page_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, course, source_course, year, sem, moed, ver, source, source_url, original_path,
         rel(dest), sha, _page_count(dest)),
    )
    con.commit()
    log.info("  + %s  (%s)", eid, source)
    return eid


# ------------------------------------------------------------------ HUJI Test Library

def _huji_meta(row: dict) -> dict:
    """Metadata from the result row: cells are name | course | year | moed | semester | file.

    The file name (course_year_semester_moed_version.pdf) carries the same info; we use it as
    the source of truth and cross-check against the cells.
    """
    name = row["url"].rsplit("/", 1)[-1].removesuffix(".pdf")
    parts = name.split("_")
    meta = {"year": UNKNOWN, "semester": UNKNOWN, "moed": UNKNOWN, "version": 1}
    if len(parts) == 5 and parts[1].isdigit():
        meta.update(year=parts[1], semester=SEMESTERS.get(parts[2], UNKNOWN),
                    moed=MOEDS.get(parts[3], UNKNOWN), version=int(parts[4]))
    cells = row["cells"]
    if len(cells) >= 5:
        heb = {"א": "A", "ב": "B", "ג": "C"}
        cell_meta = (cells[2], heb.get(cells[4], UNKNOWN), heb.get(cells[3], UNKNOWN))
        if (meta["year"], meta["semester"], meta["moed"]) != cell_meta:
            log.warning("  %s: file name and table disagree %s vs %s -> marking unknown",
                        name, (meta["year"], meta["semester"], meta["moed"]), cell_meta)
            meta.update(year=UNKNOWN, semester=UNKNOWN, moed=UNKNOWN)
    return meta


class HujiBrowser:
    def __init__(self):
        self._pw = self._browser = self.page = None
        self.limiter = RateLimiter(REQUEST_INTERVAL)

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._open()
        return self

    def _open(self):
        """(Re)open the browser window on the search form."""
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        try:
            self._browser = self._pw.chromium.launch(channel="msedge", headless=False)
        except Exception:
            log.info("Edge not available, falling back to Playwright's Chromium")
            self._browser = self._pw.chromium.launch(headless=False)
        self.page = self._browser.new_page()
        self.limiter.wait()
        self.page.goto(FORM_URL, wait_until="domcontentloaded")
        try:
            self.page.wait_for_selector("input[name=coursenum]", timeout=60_000)
            # The firewall's JS check may reload the page once; let it settle.
            self.page.wait_for_load_state("networkidle", timeout=20_000)
            self.page.wait_for_selector("input[name=coursenum]", timeout=20_000)
        except Exception as e:
            raise RuntimeError(
                "The HUJI exam search form didn't load in the browser window. The firewall may "
                "be blocking automated browsers; use the manual folder instead (see README)."
            ) from e

    def _evaluate(self, js: str, arg):
        """page.evaluate that survives the window closing / page reloading (3 attempts)."""
        from playwright.sync_api import Error as PwError

        for attempt in range(3):
            self.limiter.wait()
            try:
                return self.page.evaluate(js, arg)
            except PwError as e:
                if attempt == 2:
                    raise
                log.warning("  browser problem (%s), reopening the window (attempt %d)",
                            str(e).splitlines()[0][:80], attempt + 2)
                time.sleep(5)
                self._open()

    def __exit__(self, *exc):
        try:
            self._browser.close()
        except Exception:
            pass
        finally:
            self._pw.stop()

    def search(self, code: str) -> list[dict]:
        res = self._evaluate(SEARCH_JS, code)
        if res["blocked"]:
            raise RuntimeError("HUJI firewall rejected the search request")
        return res["rows"]

    def fetch_pdf(self, url: str, dest: Path) -> bool:
        for attempt in range(3):
            res = self._evaluate(FETCH_PDF_JS, url)
            data = base64.b64decode(res["b64"])
            if res["status"] == 200 and data[:5] == b"%PDF-":
                dest.write_bytes(data)
                return True
            log.warning("  %s: got HTTP %s (%s), retry %d", url, res["status"], res["type"], attempt + 1)
            time.sleep(5 * (attempt + 1))
        return False


def huji_rows(browser_factory, code: str, refresh: bool) -> list[dict]:
    cache = CACHE_DIR / "huji" / f"{code}.json"
    cached = read_json(cache)
    if cached and not refresh:
        age = datetime.now() - datetime.fromisoformat(cached["fetched_at"])
        if age < CACHE_TTL:
            log.info("%s: using cached search results (%d rows, %s old)", code,
                     len(cached["rows"]), str(age).split(".")[0])
            return cached["rows"]
    rows = browser_factory().search(code)
    write_json(cache, {"fetched_at": datetime.now().isoformat(timespec="seconds"), "rows": rows})
    log.info("%s: search returned %d exams", code, len(rows))
    return rows


def collect_huji(con, cfg: Config, course: str | None, refresh: bool) -> None:
    browser: HujiBrowser | None = None

    def get_browser() -> HujiBrowser:
        nonlocal browser
        if browser is None:
            log.info("Opening a visible Edge window for the HUJI Test Library...")
            browser = HujiBrowser().__enter__()
        return browser

    try:
        for c in cfg.select(course):
            for code in c.all_codes:
                rows = huji_rows(get_browser, code, refresh)
                todo = [r for r in rows if not con.execute(
                    "SELECT 1 FROM exams WHERE source_url=? UNION "
                    "SELECT 1 FROM duplicates WHERE location=?", (r["url"], r["url"])).fetchone()]
                log.info("%s (as %s): %d already downloaded, %d to fetch",
                         c.code, code, len(rows) - len(todo), len(todo))
                for r in todo:
                    with tempfile.TemporaryDirectory() as td:
                        tmp = Path(td) / "exam.pdf"
                        if not get_browser().fetch_pdf(r["url"], tmp):
                            log.error("  failed to download %s", r["url"])
                            continue
                        import_pdf(con, tmp, course=c.code, source_course=code, meta=_huji_meta(r),
                                   source="hujitest", source_url=r["url"], original_path=None)
    finally:
        if browser is not None:
            browser.__exit__(None, None, None)


# ------------------------------------------------------------------ manual folder

def collect_manual(con, cfg: Config, course: str | None) -> None:
    for c in cfg.select(course):
        base = MANUAL_DIR / c.code / "exams"
        base.mkdir(parents=True, exist_ok=True)
        pdfs = sorted(p for p in base.rglob("*") if p.suffix.lower() == ".pdf")
        new = 0
        for p in pdfs:
            orig = rel(p)
            if con.execute("SELECT 1 FROM exams WHERE original_path=? UNION "
                           "SELECT 1 FROM duplicates WHERE location=?", (orig, orig)).fetchone():
                continue
            source = "drive" if "drive" in [x.lower() for x in p.relative_to(base).parts[:-1]] else "manual"
            meta = parse_metadata_from_path(p, base)
            if import_pdf(con, p, course=c.code, source_course=c.code, meta=meta, source=source,
                          source_url=None, original_path=orig):
                new += 1
        log.info("%s: manual folder has %d PDFs, %d newly imported", c.code, len(pdfs), new)


def run(cfg: Config, course: str | None = None, refresh: bool = False, skip_huji: bool = False, **_):
    con = connect()
    sync_courses(con, cfg)
    if not skip_huji:
        collect_huji(con, cfg, course, refresh)
    collect_manual(con, cfg, course)
    con.commit()
