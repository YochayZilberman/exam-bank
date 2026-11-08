"""Shared paths, config, logging and small helpers used by every stage."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG_FILE = ROOT / "config" / "courses.yaml"
DB_PATH = DATA / "bank.db"
LOG_DIR = ROOT / "logs"

TOPICS_DIR = DATA / "topics"
EXAMS_DIR = DATA / "exams"
SOLUTIONS_DIR = DATA / "solutions"
MANUAL_DIR = DATA / "manual"
PAGES_DIR = DATA / "pages"
QUESTIONS_DIR = DATA / "questions"
SOLUTION_CROPS_DIR = DATA / "solution_crops"
CACHE_DIR = DATA / "cache"

UNKNOWN = "unknown"
# Normalised values stored in the DB.
SEMESTERS = {"1": "A", "2": "B", "3": "C"}   # HUJI codes -> A/B/C (C = summer)
MOEDS = {"1": "A", "2": "B", "3": "C"}
HEB_LETTER = {"א": "A", "ב": "B", "ג": "C"}


@dataclass
class Course:
    code: str
    name_en: str
    name_he: str
    alt_codes: list[str] = field(default_factory=list)

    @property
    def all_codes(self) -> list[str]:
        return [self.code, *self.alt_codes]


@dataclass
class Config:
    shnaton_year: int
    courses: list[Course]

    def course(self, code: str) -> Course:
        for c in self.courses:
            if code in c.all_codes:
                return c
        raise SystemExit(f"Course {code} is not in {CONFIG_FILE}")

    def select(self, code: str | None) -> list[Course]:
        return self.courses if not code else [self.course(code)]


def load_config() -> Config:
    raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    courses = [
        Course(
            code=str(c["code"]),
            name_en=c.get("name_en", ""),
            name_he=c.get("name_he", ""),
            alt_codes=[str(a) for a in c.get("alt_codes") or []],
        )
        for c in raw["courses"]
    ]
    return Config(shnaton_year=int(raw.get("shnaton_year", 2027)), courses=courses)


def get_logger(stage: str) -> logging.Logger:
    log = logging.getLogger(stage)
    if log.handlers:
        return log
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s", "%H:%M:%S")
    fh = logging.FileHandler(LOG_DIR / "pipeline.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    return log


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def rel(path: Path) -> str:
    """Path relative to the project root, with forward slashes (stored in the DB)."""
    return Path(path).resolve().relative_to(ROOT).as_posix()


def abs_path(relpath: str) -> Path:
    return ROOT / relpath


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


class RateLimiter:
    """Ensures at least `min_interval` seconds between calls to wait()."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()


# ---------------------------------------------------------------- metadata parsing

_YEAR_RE = re.compile(r"(?<!\d)(19[89]\d|20[0-4]\d)(?!\d)")
_HUJI_NAME_RE = re.compile(r"^(\d{5})_(\d{4})_([123])_([123])_(\d+)$")


def exam_id_for(course: str, year: str, semester: str, moed: str, version: int = 1) -> str:
    base = f"{course}_{year}_{semester}_{moed}"
    return base if version == 1 else f"{base}_v{version}"


def parse_metadata_from_path(path: Path, base: Path) -> dict:
    """Best-effort year/semester/moed from a manually dropped file's name and folders.

    Anything that can't be determined confidently is returned as "unknown" -- never guessed.
    Recognised forms (in the file name or any parent folder below `base`):
      * HUJI style:            83911_2024_1_2_1.pdf  (course_year_semester_moed_version)
      * year:                  2023, 2024 ...
      * moed:                  "מועד א", "מועדא", "moed a", "moed_B", "moedC"
      * semester:              "סמסטר א", "סמ א", "semester b", "sem_a", "semA"
    """
    meta = {"year": UNKNOWN, "semester": UNKNOWN, "moed": UNKNOWN, "version": 1}
    stem = path.stem
    m = _HUJI_NAME_RE.match(stem)
    if m:
        meta.update(year=m.group(2), semester=SEMESTERS[m.group(3)], moed=MOEDS[m.group(4)],
                    version=int(m.group(5)))
        return meta

    try:
        parts = list(path.relative_to(base).parts)
    except ValueError:
        parts = [path.name]
    parts[-1] = stem
    text = " / ".join(parts)
    low = text.lower()

    years = set(_YEAR_RE.findall(text))
    if len(years) == 1:
        meta["year"] = years.pop()

    moed = _find_letter(text, low, ["מועד", "moed"])
    if moed:
        meta["moed"] = moed
    sem = _find_letter(text, low, ["סמסטר", "סמ'", "סמ", "semester", "sem"])
    if sem:
        meta["semester"] = sem
    return meta


def _find_letter(text: str, low: str, keywords: list[str]) -> str | None:
    found: set[str] = set()
    for kw in keywords:
        src = low if kw.isascii() else text
        for m in re.finditer(re.escape(kw) + r"[\s_\-.']*([אבגabc])(?![a-zא-ת])", src):
            ch = m.group(1)
            found.add(HEB_LETTER.get(ch, ch.upper()))
    return found.pop() if len(found) == 1 else None
