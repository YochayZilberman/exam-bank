"""Stage 7 -- suggest topic tags + a one-line summary for each question (Claude vision).

Needs ANTHROPIC_API_KEY (put it in .env). Without it this stage logs a message and skips.

Each question's crop image(s) are sent with that course's topic list (data/topics/<course>.json).
The model must answer with 1-3 topic ids from the list plus a one-line summary. Tags are stored
as 'suggested'; questions that already have confirmed tags only get their summary filled in.
Results are cached in data/cache/tags/<hash>.json (hash of images + topic list + prompt), so
nothing is re-sent on re-runs unless the image or topic list changes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os

from pydantic import BaseModel

from .common import CACHE_DIR, TOPICS_DIR, Config, abs_path, get_logger, read_json, write_json
from .db import connect

MODEL = "claude-opus-5-5"
PROMPT_VERSION = "v1"
log = get_logger("tag")


class TagResult(BaseModel):
    topic_ids: list[str]
    summary: str


MAX_SIDE = 7900   # the API rejects images with a side over 8000 px


def fit_for_api(png: bytes) -> bytes:
    """Shrink a crop whose longest side exceeds the API limit (tall multi-page crops)."""
    import io
    from PIL import Image
    with Image.open(io.BytesIO(png)) as im:
        if max(im.size) <= MAX_SIDE:
            return png
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        out = io.BytesIO()
        im.save(out, format="PNG")
        return out.getvalue()


def topic_lines(topics: list[dict]) -> str:
    out = []
    for t in topics:
        names = " / ".join(x for x in (t.get("en"), t.get("he")) if x)
        out.append(f"{t['id']}: {names}")
    return "\n".join(out)


def build_prompt(course_name: str, topics: list[dict]) -> str:
    return (
        f"These images show one question from a past university exam in the course "
        f"\"{course_name}\" (the exam may be in Hebrew). If there are several images, they are "
        f"consecutive parts of the same question.\n\n"
        f"Course topic list (id: name):\n{topic_lines(topics)}\n\n"
        f"Choose the 1-3 topics from this list that the question mainly tests, most relevant "
        f"first, and give their ids exactly as written. Use only ids from the list. Also write a "
        f"one-line English summary of what the question asks (it is used for search, so mention "
        f"the physical system or mathematical object and what is to be found)."
    )


def run(cfg: Config, course: str | None = None, force: bool = False, **_):
    try:
        from dotenv import load_dotenv

        load_dotenv(abs_path(".env"))
    except ImportError:
        pass
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.info("ANTHROPIC_API_KEY not set (see .env.example) -- skipping topic tagging")
        return
    import anthropic

    client = anthropic.Anthropic()
    con = connect()
    for c in cfg.select(course):
        tf = read_json(TOPICS_DIR / f"{c.code}.json")
        if not tf or not tf.get("topics"):
            log.warning("%s: no topics file, run the topics stage first", c.code)
            continue
        topics = tf["topics"]
        valid = {t["id"] for t in topics}
        prompt = build_prompt(c.name_en or c.name_he, topics)
        qs = con.execute(
            "SELECT q.* FROM questions q JOIN exams e ON e.id=q.exam_id WHERE e.course=? "
            + ("" if force else "AND (q.summary IS NULL OR NOT EXISTS "
               "(SELECT 1 FROM question_tags t WHERE t.question_id=q.id))"),
            (c.code,)).fetchall()
        log.info("%s: %d questions to tag", c.code, len(qs))
        sent = cached = 0
        for q in qs:
            images = json.loads(q["images"])
            blobs = [abs_path(im["path"]).read_bytes() for im in images]
            key_src = hashlib.sha256()
            for b in blobs:
                key_src.update(b)
            key_src.update(json.dumps(topics, ensure_ascii=False, sort_keys=True).encode())
            key_src.update((MODEL + PROMPT_VERSION + prompt).encode())
            cache_file = CACHE_DIR / "tags" / f"{key_src.hexdigest()}.json"
            res = read_json(cache_file)
            if res is None:
                content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                        "data": base64.b64encode(fit_for_api(b)).decode()}}
                           for b in blobs]
                content.append({"type": "text", "text": prompt})
                try:
                    resp = client.messages.parse(
                        model=MODEL,
                        max_tokens=4000,
                        output_config={"effort": "low"},
                        messages=[{"role": "user", "content": content}],
                        output_format=TagResult,
                    )
                except anthropic.RateLimitError:
                    log.warning("rate limited; stopping for now (re-run later to continue)")
                    con.commit()
                    return
                except anthropic.APIStatusError as e:
                    log.error("%s: API error %s: %s", q["id"], e.status_code, e.message)
                    continue
                except anthropic.APIConnectionError as e:
                    log.error("%s: connection error: %s", q["id"], e)
                    continue
                if resp.stop_reason == "refusal" or resp.parsed_output is None:
                    log.warning("%s: no usable answer (stop_reason=%s)", q["id"], resp.stop_reason)
                    continue
                res = resp.parsed_output.model_dump()
                write_json(cache_file, res)
                sent += 1
            else:
                cached += 1

            ids = [t for t in res["topic_ids"] if t in valid][:3]
            con.execute("UPDATE questions SET summary=? WHERE id=?", (res["summary"].strip(), q["id"]))
            has_confirmed = con.execute("SELECT 1 FROM question_tags WHERE question_id=? AND state='confirmed'",
                                        (q["id"],)).fetchone()
            if not has_confirmed:
                con.execute("DELETE FROM question_tags WHERE question_id=? AND state='suggested'", (q["id"],))
                con.executemany("INSERT OR IGNORE INTO question_tags VALUES (?,?,'suggested')",
                                [(q["id"], t) for t in ids])
            con.commit()
        log.info("%s: tagged %d via API, %d from cache", c.code, sent, cached)
