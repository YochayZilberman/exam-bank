"""Light layout pass: find question headers on page images and cut questions out.

Only header *positions* are needed, not the full text. Words come from the PDF's text layer
when it has one, otherwise from Tesseract OCR (Hebrew + English), cached per page.

Recognised top-level headers (at the start of a line, near the margin):
    שאלה 1 / שאלה מס' 2 / שאלה מספר 3 / שאלה 4:   (Hebrew, right margin)
    Question 1 / Problem 2 / Exercise 3            (English, left margin)
Fallback, only when none of the above are found: a bare "1." / "2)" at the line start.
Sub-questions (א/ב/ג, a/b/c) are never split -- they stay inside the parent crop.
"""
from __future__ import annotations

import dataclasses
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageOps

from .common import CACHE_DIR, ROOT, read_json, write_json

DPI = 200
PAD = 18           # px above a header to include
MIN_PART_H = 60    # ignore continuation slivers shorter than this (px)
CONT_TOP = 0.06    # fraction of page height skipped at the top of continuation pages


@dataclass
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    line: tuple


@dataclass
class Header:
    page: int      # 1-based
    y: float       # top of the header line, image px
    number: int    # the reading chosen by pick_sequence
    kind: str      # heb / eng / numeric
    alts: frozenset = frozenset()   # all plausible readings of the label (OCR is noisy)
    x: float = 0.0                  # label's outer edge (x1 for RTL, x0 for LTR) -- column test
    side: str = ""                  # "rtl" / "ltr" for numeric labels
    label: int = 0                  # number as printed (differs from `number` after a restart)


# ------------------------------------------------------------------ word extraction

def tesseract_cmd() -> str | None:
    cand = [os.environ.get("TESSERACT_CMD"), shutil.which("tesseract"),
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe")]
    return next((c for c in cand if c and Path(c).exists()), None)


def text_layer_words(pdf_page: fitz.Page) -> list[Word]:
    scale = DPI / 72.0
    out = []
    for x0, y0, x1, y1, t, b, l, _ in pdf_page.get_text("words"):
        out.append(Word(t, x0 * scale, y0 * scale, x1 * scale, y1 * scale, (b, l)))
    return out


def ocr_words(img_path: Path, cache_key: str) -> list[Word]:
    cache = CACHE_DIR / "ocr" / f"{cache_key}.json"
    data = read_json(cache)
    if data is None:
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd()
        tessdata = ROOT / "tessdata"  # project-local models (heb + eng)
        if (tessdata / "heb.traineddata").exists():
            os.environ["TESSDATA_PREFIX"] = str(tessdata)
        d = pytesseract.image_to_data(Image.open(img_path), lang="heb+eng", config="--psm 3",
                                      output_type=pytesseract.Output.DICT)
        data = [
            [d["text"][i], d["left"][i], d["top"][i], d["width"][i], d["height"][i],
             d["block_num"][i], d["par_num"][i], d["line_num"][i]]
            for i in range(len(d["text"])) if d["text"][i].strip() and float(d["conf"][i]) > 20
        ]
        write_json(cache, data)
    return [Word(t, l, tp, l + w, tp + h, (b, p, ln)) for t, l, tp, w, h, b, p, ln in data]


def page_words(pdf_page: fitz.Page, img_path: Path, cache_key: str) -> tuple[list[Word], str]:
    words = text_layer_words(pdf_page)
    letters = sum(bool(re.search(r"[A-Za-zא-ת]", w.text)) for w in words)
    if letters >= 15:
        return words, "text"
    if tesseract_cmd() is None:
        raise RuntimeError("page has no text layer and Tesseract OCR is not installed")
    return ocr_words(img_path, cache_key), "ocr"


# ------------------------------------------------------------------ header detection

_PUNCT = "():.-–,;\"'״׳[]{}*"
HEB_Q = {"שאלה", "הלאש"}           # second form = reversed text layer
ENG_Q = {"question", "problem", "exercise"}
NUM_ONLY = re.compile(r"^\(?(\d{1,2})[.):]?$")


_BIDI = dict.fromkeys(map(ord, "‎‏‪‫‬‭‮⁦⁧⁨⁩﻿"))
GEMATRIA = {c: i for i, c in enumerate("אבגדהוזחטי", 1)}
# Digits that Tesseract (heb) commonly misreads as letters, seen on real exams:
# "שאלהו" == "שאלה 1", "שאלה ד:" == "שאלה 7:".
OCR_DIGIT = {"ו": {1}, "ן": {1}, "l": {1}, "I": {1}, "|": {1}, "ד": {7}, "ז": {7}}


def _clean(t: str) -> str:
    return t.translate(_BIDI).strip(_PUNCT + " ")


def _readings(t: str) -> set[int]:
    """Possible question numbers for a label token: '3', '6:מנוע', 'ד', 'ב.' ..."""
    t = _clean(t)
    m = re.match(r"^(\d{1,2})(?!\d)", t)
    if m:
        n = int(m.group(1))
        return {n} if 0 < n < 30 else set()
    if len(t) == 1:
        return ({GEMATRIA[t]} if t in GEMATRIA else set()) | OCR_DIGIT.get(t, set())
    return set()


def _num(t: str) -> int | None:
    t = _clean(t)
    if t.isdigit() and 0 < int(t) < 30:
        return int(t)
    return None


def group_lines(words: list[Word]) -> list[list[Word]]:
    lines: dict[tuple, list[Word]] = {}
    for w in words:
        lines.setdefault(w.line, []).append(w)
    return list(lines.values())


def find_headers_on_page(words: list[Word], page_no: int, page_w: float) -> list[Header]:
    if not words:
        return []
    text_right = max(w.x1 for w in words)
    text_left = min(w.x0 for w in words)
    margin = 0.22 * page_w
    found: list[Header] = []
    numeric: list[Header] = []
    for line in group_lines(words):
        top = min(w.y0 for w in line)
        rtl = sorted(line, key=lambda w: -w.x1)   # reading order for Hebrew
        ltr = sorted(line, key=lambda w: w.x0)
        toks_r = [_clean(w.text) for w in rtl[:4]]
        toks_l = [_clean(w.text).lower() for w in ltr[:3]]

        # Hebrew: "שאלה" among the first two tokens from the right, number right after it
        # (or after "מס'"/"מספר"); OCR sometimes puts the number on the other side.
        # Section banners ("חלק א", "חלק ב - העמקה", "Part B") end the previous question.
        if ((toks_r[0] in {"חלק", "קלח"} and len(toks_r) > 1 and toks_r[1][:1] in "אבגד")
                or (toks_l and toks_l[0] == "part" and len(toks_l) > 1 and len(toks_l[1]) <= 2)):
            found.append(Header(page_no, top, 0, "section"))
            continue

        # A row with several "שאלה" is a table (e.g. the grade table on a cover page).
        if sum(_clean(w.text)[:4] in HEB_Q for w in line) > 1:
            continue

        for i, t in enumerate(toks_r[:2]):
            if t[:4] not in HEB_Q or rtl[i].x1 < text_right - margin:
                continue
            if len(t) > 4:                     # label glued to the word: "שאלה6", "שאלהו"
                alts = _readings(t[4:])
            else:
                rest = toks_r[i + 1:i + 3]
                if rest and rest[0] in {"מס", "מספר", "סמ", "רפסמ"}:
                    rest = rest[1:]
                alts = _readings(rest[0]) if rest else set()
                if not alts and i > 0:          # OCR put the number on the other side
                    alts = _readings(toks_r[i - 1])
            if not alts and len(t) == 4 and len(line) <= 5:
                # Short "שאלה ..." line whose number OCR couldn't read: a header, number unknown.
                # pick_sequence accepts it as whatever number is due next.
                alts = set(range(1, 30))
            if alts:
                found.append(Header(page_no, top, min(alts), "heb", frozenset(alts)))
            break

        # English
        if toks_l and toks_l[0] in ENG_Q and ltr[0].x0 <= text_left + margin and len(toks_l) > 1:
            n = _num(toks_l[1])
            if n:
                found.append(Header(page_no, top, n, "eng", frozenset({n})))

    nums = numeric_candidates(words, page_no, page_w)
    blocks = instruction_blocks(words)
    nums = [h for h in nums if not any(a <= h.y <= b for a, b in blocks)]
    return found + nums


_INSTR = re.compile(r"הוראות|הנחיות|instructions", re.I)


def instruction_blocks(words: list[Word]) -> list[tuple[float, float]]:
    """Vertical spans of numbered instruction lists: from a "הוראות"/"הנחיות"/"Instructions"
    heading down to the first vertical gap larger than ~3 line heights."""
    lines = sorted(group_lines(words), key=lambda l: min(w.y0 for w in l))
    spans = []
    for i, l in enumerate(lines):
        if not _INSTR.search(" ".join(w.text for w in l)):
            continue
        start = min(w.y0 for w in l)
        end = max(w.y1 for w in l)
        lh = max(end - start, 20)
        for nxt in lines[i + 1:]:
            top = min(w.y0 for w in nxt)
            if top - end > 3 * lh:
                break
            end = max(end, max(w.y1 for w in nxt))
        spans.append((start, end))
    return spans


_NUM_LABEL = re.compile(r"^[.(]?(\d{1,2})[.)]?$")


def numeric_candidates(words: list[Word], page_no: int, page_w: float) -> list[Header]:
    """Bare "1." / "2)" labels at the text margin (OCR may give ".1" in RTL order, and may put
    the label on a line of its own), with text continuing on the same visual line."""
    lines = [l for l in group_lines(words) if len(l) >= 3]
    if not lines:
        return []
    rights = sorted(max(w.x1 for w in l) for l in lines)
    lefts = sorted(min(w.x0 for w in l) for l in lines)
    body_right = rights[int(0.9 * (len(rights) - 1))]   # robust to stray marks in the margin
    body_left = lefts[int(0.1 * (len(lefts) - 1))]
    tol = 0.06 * page_w
    out = []
    real = [o for o in words if re.search(r"\w", o.text)]   # ignore lone "." "," etc.
    for w in words:
        t = w.text.translate(_BIDI).strip()
        h = max(w.y1 - w.y0, 10)
        cy = (w.y0 + w.y1) / 2
        same_row = [o for o in real if o is not w and abs((o.y0 + o.y1) / 2 - cy) < h]
        left_of = [o for o in same_row if o.x1 < w.x0]
        right_of = [o for o in same_row if o.x0 > w.x1]
        m = _NUM_LABEL.match(t)
        if not m:
            # Loose candidate: outermost word on its row that starts with a number ("3.כ",
            # "(2", "4)x"). Only used if it lines up with the column of confirmed labels.
            lm = re.match(r"^[.(]?(\d{1,2})(?!\d)", t)
            if lm and left_of and not right_of:
                n = int(lm.group(1))
                out.append(Header(page_no, min([w.y0] + [o.y0 for o in same_row]), n, "loose",
                                  frozenset({n}), x=w.x1, side="rtl"))
            elif lm and right_of and not left_of:
                n = int(lm.group(1))
                out.append(Header(page_no, min([w.y0] + [o.y0 for o in same_row]), n, "loose",
                                  frozenset({n}), x=w.x0, side="ltr"))
            continue
        punct = bool(re.search(r"[.)]", t))
        if punct:
            rtl = w.x1 >= body_right - tol and bool(left_of)
            ltr = w.x0 <= body_left + tol and bool(right_of)
        else:
            # OCR often drops the period ("2" for "2."): accept a bare number only in the
            # hanging-indent shape -- outermost word on its row, at the margin, then a gap.
            gap = 15
            rtl = (not right_of and left_of and w.x1 >= body_right - 0.02 * page_w
                   and w.x0 - max(o.x1 for o in left_of) >= gap)
            ltr = (not left_of and right_of and w.x0 <= body_left + 0.02 * page_w
                   and min(o.x0 for o in right_of) - w.x1 >= gap)
        if rtl or ltr:
            n = int(m.group(1))
            top = min([w.y0] + [o.y0 for o in same_row])
            out.append(Header(page_no, top, n, "numeric", frozenset({n}),
                              x=w.x1 if rtl else w.x0, side="rtl" if rtl else "ltr"))
        elif left_of and not right_of or right_of and not left_of:
            n = int(m.group(1))   # number-shaped but not at the margin: column test decides
            out.append(Header(page_no, min([w.y0] + [o.y0 for o in same_row]), n, "loose",
                              frozenset({n}), x=w.x1 if left_of else w.x0,
                              side="rtl" if left_of else "ltr"))
    return out


COLUMN_TOL = 20  # px: labels of one exam line up vertically
SHIFT_TOL = 60   # px: how far a scanned page may be shifted relative to the others


def label_column(cands: list[Header], sections: list[Header]):
    """(side, global_col, {page: col}) of the bare-number question labels, or None.

    The global column is the median outer edge of a first-pass sequence of strict labels. On
    each page the column is the strict label closest to it (within SHIFT_TOL) -- so a page
    shifted by the scanner still works, while sub-items indented inward (e.g. an "ב." that OCR
    read as "2.") are not taken as the column.
    """
    strict = [h for h in cands if h.kind == "numeric"]
    seq, _ = pick_sequence([dataclasses.replace(h) for h in strict], sections)
    if len(seq) < 2:
        return None
    side = max(("rtl", "ltr"), key=lambda s: sum(h.side == s for h in seq))
    xs = sorted(h.x for h in seq if h.side == side)
    global_col = xs[len(xs) // 2]
    page_col: dict[int, float] = {}
    for h in strict:
        if h.side != side or abs(h.x - global_col) > SHIFT_TOL:
            continue
        cur = page_col.get(h.page)
        if cur is None or abs(h.x - global_col) < abs(cur - global_col):
            page_col[h.page] = h.x
    return side, global_col, page_col


def strip_labels(img_path: Path, page_no: int, col: float, side: str, cache_key: str) -> list[Header]:
    """Re-OCR only the narrow label column of a page, digits only.

    Full-page OCR often drops an isolated "2." next to a block of Hebrew; on its own in a strip
    it is usually read fine. Results are cached.
    """
    cache = CACHE_DIR / "ocr_strip" / f"{cache_key}_{int(col)}.json"
    data = read_json(cache)
    img = Image.open(img_path)
    if side == "rtl":
        x0, x1 = int(col - 75), int(col + 20)
    else:
        x0, x1 = int(col - 20), int(col + 75)
    x0, x1 = max(0, x0), min(img.width, x1)
    if data is None:
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd()
        strip = img.crop((x0, 0, x1, img.height))
        d = pytesseract.image_to_data(
            strip, lang="eng", output_type=pytesseract.Output.DICT,
            config="--psm 6 -c tessedit_char_whitelist=0123456789.()")
        data = [[d["text"][i], d["left"][i], d["top"][i], d["width"][i], d["height"][i]]
                for i in range(len(d["text"])) if d["text"][i].strip() and float(d["conf"][i]) > 30]
        write_json(cache, data)
    out = []
    for t, l, tp, w, h in data:
        m = _NUM_LABEL.match(t.strip())
        if m and 0 < int(m.group(1)) < 30:
            n = int(m.group(1))
            out.append(Header(page_no, tp, n, "numeric", frozenset({n}),
                              x=x0 + l + w if side == "rtl" else x0 + l, side=side))
    return out


def numeric_sequence(cands: list[Header], sections: list[Header], col=None) -> tuple[list[Header], bool]:
    """Pick 1,2,3,... from bare-number labels, using column alignment.

    First pass uses strict labels only. The column (outer x edge) of the labels found is then
    used to admit loose candidates lined up with it, and the sequence is re-picked. Any
    number-like label in that column that still isn't explained means a missed header (gap).
    """
    col = col or label_column(cands, sections)
    if col is None:
        seq, _ = pick_sequence([dataclasses.replace(h) for h in cands if h.kind == "numeric"], sections)
        return seq, False
    side, global_col, page_col = col
    in_col = [h for h in cands if h.kind in ("numeric", "loose") and h.side == side
              and abs(h.x - page_col.get(h.page, global_col)) <= COLUMN_TOL]
    seq, _ = pick_sequence([dataclasses.replace(h) for h in in_col], sections)
    if not seq:
        return seq, False
    # Gap: after the last accepted label (local number L), a label in the same column numbered
    # above L+1 -- so at least one header in between was missed.
    last = seq[-1]
    gap = any((h.page, h.y) > (last.page, last.y) and min(h.alts) > last.label + 1
              and min(h.alts) <= last.label + 4 for h in in_col)
    # After a section banner the first label in the column should start the section ("1.")
    # or continue the count. If that first label is a "1." we didn't use, the sections were
    # mis-split (typically: labels in the previous section were missed) -> manual.
    picked = {(h.page, h.y) for h in seq}
    for s in sections:
        after = sorted((h for h in in_col if (h.page, h.y) > (s.page, s.y)), key=lambda h: (h.page, h.y))
        if after and 1 in after[0].alts and (after[0].page, after[0].y) not in picked \
                and not any(abs(after[0].y - h.y) < 15 and after[0].page == h.page for h in seq):
            gap = True
    return seq, gap


def pick_sequence(cands: list[Header], sections: list[Header] = ()) -> tuple[list[Header], bool]:
    """Keep the run 1,2,3,... in reading order; drop repeats like 'המשך שאלה 1'.

    * A candidate is accepted if the expected number is one of its plausible readings, so an
      OCR'd "שאלה ד" is accepted as question 7 only right after question 6.
    * After a section banner ("חלק ב") numbering may restart at 1; those questions continue the
      running count (part B's "1." becomes question 4 if part A had 3).
    Returns (sequence, gap) -- gap=True when a later number was seen but an earlier one is
    missing (e.g. 1,2,4): the crops would be wrong, so the caller marks the exam manual.
    """
    cands = sorted(cands, key=lambda h: (h.page, h.y))
    sec_pos = sorted((s.page, s.y) for s in sections)
    seq: list[Header] = []
    expect, offset, last_sec = 1, 0, None
    for h in cands:
        pos = (h.page, h.y)
        passed = [s for s in sec_pos if s < pos]
        if seq and passed and passed[-1] != last_sec and 1 in (h.alts or {h.number}) and expect > 2:
            offset, expect, last_sec = len(seq), 1, passed[-1]   # restart inside a new section
        if expect in (h.alts or {h.number}):
            h.number = offset + expect
            h.label = expect
            seq.append(h)
            expect += 1
    accepted = {id(h) for h in seq}
    # Gap: an unaccepted "שאלה N"/"Question N" whose only reading is beyond the run -- a header
    # in the run was missed.
    gap = any(id(h) not in accepted and h.alts and min(h.alts) > expect and len(h.alts) == 1
              and h.kind != "numeric" for h in cands)
    return seq, gap


def detect_headers(pdf_path: Path, page_imgs: list[Path], cache_prefix: str):
    """Returns (headers, note, last_exam_page, margins). Empty headers = detection failed.

    margins: {page: (content_top, content_bottom|None)} -- see page_margins().
    """
    all_cands: list[Header] = []
    sources = set()
    end_page = len(page_imgs)
    margins: dict[int, tuple[int, int | None]] = {}
    with fitz.open(pdf_path) as doc:
        for i, page in enumerate(doc, 1):
            img = page_imgs[i - 1]
            words, src = page_words(page, img, f"{cache_prefix}/page_{i}")
            sources.add(src)
            w_px, h_px = Image.open(img).size
            page_cands = find_headers_on_page(words, i, w_px)
            has_questions = any(h.kind in ("heb", "eng") for h in page_cands)
            # Attached formula sheet: the exam ends before it. A page that also has question
            # headers (e.g. "תזכורת נוסחאות" above question 1) is not one.
            if i > 1 and not has_questions and is_formula_sheet(words, h_px):
                end_page = i - 1
                break
            margins[i] = page_margins(words, h_px)
            all_cands += page_cands
    # Prefer real "שאלה N"/"Question N" headers; use numeric ones only if those fail.
    sections = [h for h in all_cands if h.kind == "section"]
    strong_cands = [h for h in all_cands if h.kind in ("heb", "eng")]
    strong, gap = pick_sequence(strong_cands, sections)
    definite = [h for h in strong_cands if len(h.alts) == 1]
    if len(strong) >= 2:
        seq = strong
    elif len(definite) >= 2:
        # The exam clearly uses "שאלה N" headers but the run broke: don't fall back to bare
        # numbers (they'd pick up instruction lists) -- send it to manual cropping.
        seq, gap = strong, True
    else:
        col = label_column(all_cands, sections)
        seq, gap = numeric_sequence(all_cands, sections, col)
        if col is not None:
            # Second chance for labels full-page OCR dropped: OCR the label column itself.
            # (The column is fixed from the full-page labels so strip noise can't move it.)
            side, global_col, page_col = col
            extra = []
            for p in range(1, end_page + 1):
                extra += strip_labels(page_imgs[p - 1], p, page_col.get(p, global_col), side,
                                      f"{cache_prefix}/page_{p}")
            seq, gap = numeric_sequence(all_cands + extra, sections, col)
    if gap:
        return [], (f"header detection found a gap in the numbering after question {len(seq)} "
                    f"-- a header was probably missed"), end_page, margins
    if len(seq) >= 2:
        # Section banners after the first question act as boundaries (crop_questions skips them).
        first = (seq[0].page, seq[0].y)
        seq = sorted(seq + [h for h in all_cands if h.kind == "section" and (h.page, h.y) > first],
                     key=lambda h: (h.page, h.y))
    note = f"{sum(h.kind != 'section' for h in seq)} headers via {'/'.join(sorted(sources))}"
    if end_page < len(page_imgs):
        note += f"; pages {end_page + 1}-{len(page_imgs)} look like a formula sheet, excluded"
    if len(seq) < 2:
        return [], f"header detection failed ({note})", end_page, margins
    return seq, note, end_page, margins


_RUNNING = re.compile(r"\d{5}|מבחן|מועד|תשפ|exam|moed|ת\.?ז|ת״ז|ת\"ז|\bID\b", re.I)
_FOOTER = re.compile(r"^(\d{1,2}|עמוד\s*\d+.*|page\s*\d+.*|\d+\s*/\s*\d+|-\s*\d+\s*-)$", re.I)


def page_margins(words: list[Word], page_h: float) -> tuple[int, int | None]:
    """(content_top, content_bottom) for a page: below a running header, above a page number.

    Running header = the topmost line, within the top 20% of the page, mentioning the course
    number / "מבחן" / "מועד" / year. Footer = the bottom line, within the bottom 15%, that is just
    a page number ("3", "עמוד 3 מתוך 5", "3/5"). Only used on continuation pages.
    """
    lines = sorted(group_lines(words), key=lambda l: min(w.y0 for w in l))
    top, bottom = 0, None
    if lines:
        first = lines[0]
        text = " ".join(_clean(w.text) for w in sorted(first, key=lambda w: -w.x1))
        y1 = max(w.y1 for w in first)
        if y1 < 0.2 * page_h and _RUNNING.search(text):
            top = int(y1 + 15)  # also clears the rule line usually drawn under it
        last = lines[-1]
        text = " ".join(_clean(w.text) for w in sorted(last, key=lambda w: -w.x1)).strip()
        y0 = min(w.y0 for w in last)
        if y0 > 0.85 * page_h and _FOOTER.match(text):
            bottom = int(y0 - 10)
    return top, bottom


_FORMULA = re.compile(r"נוסחאות|נוסחאת|formulas?\b|formula\s*sheet|identities|equation\s*sheet", re.I)


def is_formula_sheet(words: list[Word], page_h: float) -> bool:
    """A page whose title (first 3 text lines, within the top 22%) names a formula sheet.

    Matches "נוסחאות" alone because OCR often garbles "דף" (seen as "AT")."""
    lines = sorted((l for l in group_lines(words) if re.search(r"\w{2}", " ".join(w.text for w in l))),
                   key=lambda l: min(w.y0 for w in l))
    title = " ".join(" ".join(_clean(w.text) for w in l) for l in lines[:3]
                     if min(w.y0 for w in l) < 0.22 * page_h)
    return bool(_FORMULA.search(title))


# ------------------------------------------------------------------ cropping

def _content_box(img: Image.Image, threshold: int = 200):
    """Bounding box of dark pixels, or None for a blank region."""
    inv = ImageOps.invert(img.convert("L")).point(lambda v: 255 if v > 255 - threshold else 0)
    return inv.getbbox()


def crop_questions(headers: list[Header], page_imgs: list[Path], out_dir: Path,
                   prefix: str = "q", skip: set[int] = frozenset(),
                   margins: dict | None = None) -> dict[int, list[dict]]:
    """Cut each question from its header to the next header / end of document.

    Returns {number: [{page, path, bbox}, ...]} with paths absolute.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    sizes = [Image.open(p).size for p in page_imgs]
    n_pages = len(page_imgs)
    result: dict[int, list[dict]] = {}
    for k, h in enumerate(headers):
        if h.kind == "section" or h.number in skip:
            continue
        nxt = headers[k + 1] if k + 1 < len(headers) else None
        end_page, end_y = (nxt.page, nxt.y - PAD) if nxt else (n_pages, None)
        parts = []
        for p in range(h.page, end_page + 1):
            w, hgt = sizes[p - 1]
            m_top, m_bottom = (margins or {}).get(p, (0, None))
            # Continuation pages: skip the running header strip at the top.
            y0 = max(0, h.y - PAD) if p == h.page else max(int(CONT_TOP * hgt), m_top)
            # (the last ~1.5% of a scan often holds a scanner edge line, not content)
            y1 = end_y if (p == end_page and end_y is not None) else (m_bottom or int(0.985 * hgt))
            if y1 - y0 < MIN_PART_H:
                continue
            img = Image.open(page_imgs[p - 1])
            region = img.crop((0, int(y0), w, int(y1)))
            box = _content_box(region)
            if box is None:
                continue  # blank (e.g. empty space at the end of a page)
            bx0, by0, bx1, by1 = box
            pad = 12
            bbox = [max(0, bx0 - pad), int(y0) + max(0, by0 - pad),
                    min(w, bx1 + pad), int(y0) + min(region.height, by1 + pad)]
            if bbox[3] - bbox[1] < MIN_PART_H // 2:
                continue
            if p != h.page and bbox[3] - bbox[1] < 90 and bbox[1] < 0.2 * hgt:
                continue  # sliver at the top of a continuation page: a header line, not content
            parts.append({"page": p, "bbox": bbox})
        for i, part in enumerate(parts, 1):
            path = out_dir / f"{prefix}{h.number}_{i}.png"
            Image.open(page_imgs[part["page"] - 1]).crop(tuple(part["bbox"])).save(path)
            part["path"] = path
        result[h.number] = parts
    return result
