# HUJI past-exam question bank

A personal, local-only question bank: past exams are collected, split into one image per
question, tagged with syllabus topics, and stored in SQLite for a Notion-style study site.
The exams and solutions aren't ours to redistribute, so nothing here is meant to be published.

## Setup (once)

```powershell
cd C:\Users\yocha\exam-bank
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env      # then put your ANTHROPIC_API_KEY in .env (for topic tagging)
```

Also needed:
* **Microsoft Edge**, which Windows already has. The HUJI collector drives a visible Edge window.
* **Tesseract OCR** for scanned exams: `winget install UB-Mannheim.TesseractOCR`.
  The Hebrew model is kept in `tessdata/` inside the project, so the default install is enough.

## The study site

```powershell
cd C:\Users\yocha\exam-bank
.\.venv\Scripts\python.exe serve.py        # opens http://127.0.0.1:8000 in your browser
```

The site only listens on this computer. Stop it with Ctrl+C. It reads and writes `data/bank.db`
directly, so you can leave it running while the pipeline runs; refresh the page afterwards.

**Views**
* **All questions:** a table with filters (course, topic, year, moed, status, crop status),
  sortable columns (click a header) and search. Press `/` to jump to search.
* **By topic:** questions grouped by topic tag, shown as cards.
* **Course pages** (sidebar): progress stats plus that course's questions.
* **Practice:** picks random unsolved questions from the topics you choose. Mark each one as you
  go.

Click any question to open its panel. It shows:
* the question image(s), plus links to the original exam PDF at the right page and to the
  solution;
* status, difficulty (1–5) and notes, which save automatically;
* the topic tags, where you confirm suggested ones or add and remove your own;
* a "Looks right" button that marks the crop as verified.

Press `j` / `k` to move to the next or previous question, and `Esc` to close the panel.

**Fix-up tools** (sidebar; the red badges count what's waiting)
* **Cropping:**
  * Shows a queue of exams that need manual cropping, then auto-crops not yet verified.
  * Drag on a page to draw a box. A question that continues on the next page gets one box per
    page, in order.
  * Set the question number and click Save. Use "Redraw" or ✕ to fix existing crops.
  * "All correct — mark exam done" verifies the exam and stops the pipeline from re-cropping it.
* **Tag review:** confirm the topics suggested by the tagging stage, one by one or all at once.
* **Exam metadata:** fix year, semester, moed and lecturer. Exams with unknown fields are listed
  first.
* **Solutions:** assign solution PDFs that couldn't be matched to an exam automatically.

## Running the pipeline

```powershell
.\.venv\Scripts\python.exe run.py                     # everything, all courses
.\.venv\Scripts\python.exe run.py all --course 83911  # everything, one course
.\.venv\Scripts\python.exe run.py exams --course 80431
.\.venv\Scripts\python.exe run.py status              # what's in the database
```

| Stage | Command | What it does |
|---|---|---|
| 1 | `topics` | Fetches syllabus topics from the Shnaton JSON API and writes them to `data/topics/<course>.json` |
| 2 | `exams` | Collects exams from the HUJI Test Library (visible browser) and `data/manual/<course>/exams/` into `data/exams/<course>/` |
| 3 | `solutions` | Imports `data/manual/<course>/solutions/` and matches each solution to its exam |
| 4 | `render` | Renders every page to `data/pages/<id>/page_<n>.png` at 200 DPI |
| 5 | `crop` | Finds the question headers and crops each question to `data/questions/<exam_id>/q<n>_<part>.png` |
| 6 | `crop-solutions` | Does the same for solutions; if splitting fails, links the solution PDF page instead |
| 7 | `tag` | Asks Claude (vision) for 1–3 topic ids and a one-line summary per question, stored as *suggested* |
| 8 | `build` | Builds the `question_rows` view and the full-text index in `data/bank.db` |

Every stage is incremental: it skips work that's already done and logs to `logs/pipeline.log`.

Flags:
* `--force`: redoes skipped work. It regenerates topic files, re-crops auto crops and re-tags.
* `--refresh`: ignores cached web responses.
* `--skip-huji`: uses the manual folder only, without opening the browser.

**Never overwritten by re-runs:**
* your status, difficulty and notes;
* crops you verified or drew by hand;
* confirmed tags;
* manual solution matches;
* hand-fixed exam metadata;
* edited topic files (unless you pass `--force` to `topics`).

## Sources

* **HUJI Test Library** (`www4.huji.ac.il/htbin/exams/exams.cgi`) covers the Faculty of Science
  only.
  * Its firewall rejects plain HTTP scripts, so stage 2 opens a visible Edge window and makes
    the requests from inside it, about one every 4 seconds. Search results are cached for 7 days.
  * Metadata comes from the result table and the file name
    (`course_year_semester_moed_version.pdf`).
  * The site has no lecturer field, so lecturer stays `unknown`.
* **Shnaton** (`shnaton.huji.ac.il/api/syllabus?courseCode=…&year=…`) is a public JSON API.
* **Aguda academic bank:** not used. Solutions imported from it on 2026-11-07 were removed at the
  user's request the same day (`tools/delete_aguda.py`). Identical files that also exist in the
  Drive stay, as Drive solutions.
* **Engineering Google Drive** (shared "Semester A" folder) needs your HUJI Google account.
  * On 2026-11-07 the course exam folders were downloaded through the built-in browser. Their
    listing is in `data/cache/drive/listing.json`.
  * That browser deletes its download files within minutes. `tools/grab_downloads.py` copies
    them to `data/cache/drive/incoming/` as they arrive.
  * `tools/sort_drive.py` then reads year and moed from the Drive's own naming (`2014A`,
    `2021_MoedB`, `moed_alef`, `2024-25` → 2025). It sends solutions to `solutions/drive/` and
    exams to `exams/drive/`, and merges photo-only exams (WhatsApp pictures) into one PDF.
  * 51 of the 267 listed files were never downloaded; they were skipped by choice.
  * Handwritten CamScanner scans of student solutions were moved to `solutions/drive/`.
* A Drive or manual exam with **no semester** is treated as a duplicate when the bank already has
  the same course, year and moed (it's listed in the `duplicates` table, so you can check it).

## Dropping in files manually

```
data/manual/<course>/exams/            exam PDFs (any sub-folders)
data/manual/<course>/exams/drive/      ...files here are recorded as source = drive
data/manual/<course>/solutions/        solution PDFs
```

Year, semester and moed are read from the file name and its folder names. Recognised forms:
* `83911_2024_1_2_1.pdf`, the HUJI style;
* a year such as `2023`;
* `מועד א` / `moed B` / `moedC`;
* `סמסטר א` / `semester b` / `semA`.

Anything that can't be determined is stored as `unknown`; fix it in the site's metadata editor.
Identical files are imported only once. If two sources have the same course, year, semester and
moed, the first one is kept and the other is listed in the `duplicates` table.

## How auto-cropping decides (stage 5)

Page images are OCR'd with Tesseract (Hebrew + English) and the results are cached in
`data/cache/ocr/`. Question headers are found in this order:
1. **"שאלה N"** or **"Question N"** at the start of a line, near the margin. Common OCR misreads
   are handled ("שאלהו" = שאלה 1, "שאלה ד" = שאלה 7). A table row with several "שאלה", such as
   a cover-page grade table, is ignored.
2. If an exam has none of those, **bare "1." / "2)" labels** in the margin. They must line up in
   one column on each page. The label column is then re-OCR'd on its own to recover dropped
   labels. Numbered "הוראות" instruction lists are ignored.

A few more rules:
- After a "חלק ב" banner, numbering may restart at 1; those questions continue the count.
- An attached formula sheet, recognised by a "נוסחאות" / "formula" / "identities" title, ends
  the exam. Running page headers and ID lines are trimmed off continuation pages.

**Rather than guessing, an exam is marked "needs manual cropping"** when:
- fewer than 2 headers are found;
- the numbering has a gap (e.g. "שאלה 5" was found but "שאלה 3" wasn't);
- a section's "1." doesn't fit the sequence.

Every auto-crop starts as `unverified`. Check it in the site's cropping tool, which shows the
queue.

## Adding a course

Add a block to `config/courses.yaml`:

```yaml
  - code: "12345"
    name_en: Some Course
    name_he: קורס כלשהו
    alt_codes: ["12340"]   # old numbers to search and merge too (optional)
```

Then run `run.py all --course 12345`. Update `shnaton_year` when the new catalog is published.

## Editing topics

`data/topics/<course>.json` has a `topics` list of `{id, he, en}`. You can rename, merge, split
or add topics. Keep the ids unique, because tags point to them. Removing a topic leaves its old
tags as unknown ids, and the tag editor will show them.
