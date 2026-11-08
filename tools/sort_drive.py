"""Move Drive downloads (nameless .tmp files in ~/Downloads) into the manual folders.

data/cache/drive/listing.json holds the chosen Drive files (course, path, id, mime, size),
written while browsing the folder. Each download is identified by its exact byte size.

Each file is renamed "<year> מועד X [סמסטר Y] <original name>.pdf" when year/moed can be read
from its Drive name or folders (Drive uploaders write 2014A, 2021_MoedB, moed_alef, "2013 a
sol" ...), so the pipeline's own name parser picks them up. Anything that can't be read is
left out of the name and ends up "unknown" -- never guessed. An academic-year range such as
2023-24 counts as unknown year.

Files whose name says solution (sol / פתרון / answers) go to solutions/drive/, the rest to
exams/drive/. Photos (jpg/png) in one folder are merged, in name order, into one PDF named
after the folder (e.g. WhatsApp photos of an exam).

    python tools/sort_drive.py          # dry run
    python tools/sort_drive.py --move
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import pymupdf as fitz

ROOT = Path(__file__).resolve().parents[1]
DOWNLOADS = ROOT / "data/cache/drive/incoming"   # filled by tools/grab_downloads.py
CODE = {"80430": "80431"}
SOL_RE = re.compile(r"sol|פתרון|פיתרון|answers|ans\b", re.I)
YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
RANGE_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*-\s*(?:19|20)?\d{2}(?!\d)")
HEB = {"א": "A", "ב": "B", "ג": "C"}
WORD = {"alef": "A", "bet": "B", "gimel": "C"}
OUT_HEB = {"A": "א", "B": "ב", "C": "ג"}
BAD = '<>:"|?*/\\'
HUJI_RE = re.compile(r"^(\d{5})_((?:19|20)\d{2})_([123])_([123])_\d")
# Whole-year bundles of every moed; the single exams are in the folder too.
SKIP_RE = re.compile(r"טפסים ופתרונות|טופס ופתרון|טופס ופתרונות")


def year_of(text: str):
    """One year in the text. An academic-year range "2021-2022" / "2024-25" is given as its
    second year, the convention HUJI uses (the Drive's own "2022" folder holds the
    "2021-2022" exams)."""
    for m in RANGE_RE.finditer(text):
        a, b = re.split(r"\s*-\s*", m.group(0))
        end = a[:2] + b[-2:] if len(b) == 2 else b
        if int(end) == int(a) + 1:                 # a real academic year, not a date 2026-01-18
            return end
    text = re.sub(r"(?:19|20)\d{2}-\d{2}-\d{2}", " ", text)   # drop dates
    ys = set(YEAR_RE.findall(text))
    return ys.pop() if len(ys) == 1 else None


def moed_of(text: str):
    found = set()
    for m in re.finditer(r"מועד\s*([אבג])", text):
        found.add(HEB[m.group(1)])
    low = text.lower()
    for m in re.finditer(r"(?:moed|mode|moad)[\s_\-]*(alef|bet|gimel|[abc])(?![a-z])", low):
        found.add(WORD.get(m.group(1), m.group(1).upper()))
    for m in re.finditer(r"sol(?:ution)?[\s_\-]*([abc])(?![a-z])", low):   # 2021_SolutionB, 2016_Sol_B
        found.add(m.group(1).upper())
    if not found:  # "2014A", "2015_B_Sol", "2013 a sol", "2017ב", "10 א"
        for m in re.finditer(r"(?<![a-z0-9])(?:19|20)?\d{2}[\s_\-]*([abcאבג])(?![a-zא-ת0-9])", low):
            ch = m.group(1)
            found.add(HEB.get(ch, ch.upper()))
    return found.pop() if len(found) == 1 else None


def sem_of(text: str):
    low = text.lower()
    found = {HEB[m.group(1)] for m in re.finditer(r"סמסטר\s*([אב])", text)}
    found |= {m.group(1).upper() for m in re.finditer(r"sem(?:ester)?[\s_\-]*([ab])(?![a-z])", low)}
    if "fall" in low or "winter" in low:
        found.add("A")
    if "spring" in low:
        found.add("B")
    return found.pop() if len(found) == 1 else None


def meta_of(path: str):
    """Year/moed/semester from the file name first, then the nearest folder that has it."""
    parts = path.split("/")[1:]                   # drop course prefix
    stem = parts[-1].rsplit(".", 1)[0]
    levels = [stem] + parts[-2::-1]
    out = {}
    h = HUJI_RE.match(stem)           # 83335_2016_1_1_2_sol(86): course_year_sem_moed_version
    if h:
        return {"course": h.group(1), "year": h.group(2), "sem": "ABC"[int(h.group(3)) - 1],
                "moed": "ABC"[int(h.group(4)) - 1]}
    for key, fn in (("year", year_of), ("moed", moed_of), ("sem", sem_of)):
        for lv in levels:
            v = fn(lv)
            if v:
                out[key] = v
                break
    # The file name and its folders disagree on the year (e.g. "2024A" inside "2024-25", or
    # 2022 files inside a "2017ב" folder): leave the year unknown rather than pick one.
    fy, dy = year_of(stem), next((year_of(p) for p in parts[-2::-1] if year_of(p)), None)
    if fy and dy and fy != dy:
        out.pop("year", None)
    return out


def clean_tail(stem: str) -> str:
    t = RANGE_RE.sub(" ", stem)
    t = YEAR_RE.sub(" ", t)
    t = re.sub(r"(?i)mo?ed?|מועד|סמסטר|semester|sem(?![a-z])", " ", t)
    t = "".join("_" if ch in BAD else ch for ch in t)
    return re.sub(r"\s+", " ", t).strip(" _-.") or "file"


def target(course: str, path: str, is_sol: bool, uid: str) -> Path:
    m = meta_of(path)
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    bits = [m.get("year") or "", f"מועד {OUT_HEB[m['moed']]}" if m.get("moed") else "",
            f"סמסטר {OUT_HEB[m['sem']]}" if m.get("sem") else "", clean_tail(stem), uid[:6]]
    name = " ".join(b for b in bits if b) + ".pdf"
    kind = "solutions" if is_sol else "exams"
    course = m.get("course", course)              # a file filed under the wrong course
    return ROOT / "data/manual" / CODE.get(course, course) / kind / "drive" / name


def main(move: bool):
    items = json.loads((ROOT / "data/cache/drive/listing.json").read_text(encoding="utf-8"))
    by_size = {int(it["size"]): it for it in items}
    tmps = {f.stat().st_size: f for f in DOWNLOADS.glob("*.tmp")}
    photos = defaultdict(list)
    got = 0
    for size, it in by_size.items():
        f = tmps.get(size)
        if not f:
            continue
        name = it["path"].rsplit("/", 1)[-1]
        if SKIP_RE.search(name):
            print("skip bundle:", it["path"])
            if move:
                f.unlink()
            continue
        if it["mime"].startswith("image/"):
            photos[(it["course"], it["path"].rsplit("/", 1)[0])].append((name, f, it["id"]))
            continue
        if f.read_bytes()[:5] != b"%PDF-":
            print("not a PDF:", it["path"])
            continue
        is_sol = bool(SOL_RE.search(name)) or bool(re.search(r"/sol\s*/", it["path"], re.I))
        dest = target(it["course"], it["path"], is_sol, it["id"])
        print(f"{'SOL ' if is_sol else 'EXAM'} {it['path']}\n     -> {dest.name}")
        got += 1
        if move:
            dest.parent.mkdir(parents=True, exist_ok=True)
            f.replace(dest)
    for (course, folder), files in photos.items():
        is_sol = bool(SOL_RE.search(folder.rsplit("/", 1)[-1]))
        # Read year/moed from the photo names when they carry them ("2014b part a.jpg"),
        # otherwise from the folders (WhatsApp/uuid names have none; dates are ignored).
        first = sorted(files)[0][0]
        src = folder + "/" + first if meta_of(folder + "/" + first).get("year") else folder + ".pdf"
        dest = target(course, src, is_sol, files[0][2])
        print(f"{'SOL ' if is_sol else 'EXAM'} {len(files)} photos in {folder}\n     -> {dest.name}")
        got += 1
        if move:
            dest.parent.mkdir(parents=True, exist_ok=True)
            out = fitz.open()
            for _, f, _ in sorted(files):
                kind = "jpeg" if f.read_bytes()[:2] == b"\xff\xd8" else "png"
                with fitz.open(f, filetype=kind) as img:
                    out.insert_pdf(fitz.open("pdf", img.convert_to_pdf()))
            out.save(dest)
            out.close()
            for _, f, _ in files:
                f.unlink()
    missing = [it["path"] for s, it in by_size.items() if s not in tmps]
    print(f"\n{got} outputs; {len(missing)} of {len(by_size)} files not downloaded yet")
    for m in missing[:20]:
        print("  missing:", m)


if __name__ == "__main__":
    main("--move" in sys.argv)
