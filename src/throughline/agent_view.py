"""Agent-sized responses: what an agent reads, not what the database holds.

Agents read every response in a context window, so a read that returns a
full-text vector, a payload hash or a 50 kB email per row wastes the window
and hides the rows after it (clients cut long output). shape_rows drops
columns no reader needs, cuts long text cells with a note saying how to read
the rest, and stops adding rows at a character budget, reporting what it
left out. Nothing is hidden that the caller cannot ask for: raw=true returns
the rows unshaped.
"""

import json
import re

# Columns that carry no meaning for a reader (search internals, hashes).
DROP_COLUMNS = frozenset({"tsv", "payload_hash"})
DEFAULT_MAX_CELL = 4000
DEFAULT_MAX_CHARS = 45000


def _cut(value, limit, key):
    if isinstance(value, str) and len(value) > limit:
        return (
            value[:limit]
            + f" [... cut at {limit} of {len(value)} chars; read the rest with"
            f" substr({key}, {limit + 1}, {limit})]"
        ), True
    return value, False


def shape_rows(rows, max_cell=DEFAULT_MAX_CELL, max_chars=DEFAULT_MAX_CHARS):
    """(rows, info): rows without reader-useless columns, long cells cut, at
    most about max_chars of JSON; info counts dropped rows and cut cells."""
    out, size, cut_cells = [], 0, 0
    for index, row in enumerate(rows):
        if isinstance(row, dict):
            shaped = {}
            for key, value in row.items():
                if key in DROP_COLUMNS:
                    continue
                value, was_cut = _cut(value, max_cell, key)
                cut_cells += was_cut
                shaped[key] = value
        else:
            shaped = row
        encoded = len(json.dumps(shaped, default=str))
        if out and size + encoded > max_chars:
            return out, dict(rows_left_out=len(rows) - index, cells_cut=cut_cells,
                             max_chars=max_chars)
        out.append(shaped)
        size += encoded
    return out, dict(rows_left_out=0, cells_cut=cut_cells, max_chars=max_chars)


# --- query-matched excerpts ---------------------------------------------------

STOP = set(
    "a an and are as at be been but by can could did do does for from had has have "
    "he her him his how i if in into is it its me my of on or our she so than that "
    "the their them then there they this to was we were what when where which who "
    "why will with would you your about after before any all just not no yes".split()
)
def _terms(query):
    words = re.findall(r"[\w']+", (query or "").lower())
    return [w for w in words if len(w) > 2 and w not in STOP]


URL = re.compile(r"https?://[^\s)\]>\"']{48,}")


def tidy(text):
    """Long tracking URLs shortened; invisible padding characters removed."""
    text = URL.sub(lambda m: m.group(0)[:48] + "...", text or "")
    return re.sub(r"[\u00ad\u034f\u200b-\u200f\u2060\ufeff]+", "", text)


def snippet(text, query, limit):
    """The part of text (about limit chars) that best matches the query terms.
    For long documents with matches in two distant sections, includes both."""
    text = tidy(text)
    if len(text) <= limit:
        return text, False
    terms = _terms(query)
    lower = text.lower()
    win = int(limit * 0.65) if limit >= 1200 and len(text) > limit * 1.5 and terms else limit
    step = max(200, win // 4)
    scored = []
    for start in range(0, max(1, len(text) - win + step), step):
        window = lower[start : start + win]
        matched = {t for t in terms if t in window}
        score = sum(window.count(t) for t in terms) + len(matched) * 3
        scored.append((score, start, matched))
    scored.sort(key=lambda x: (-x[0], x[1]))
    best_score, best, best_matched = scored[0]
    second = None
    if win < limit and best_score > 0:
        win2 = limit - win - 20
        if win2 >= 350:
            for sc, st, matched in scored[1:]:
                if sc <= 0:
                    break
                if abs(st - best) < win:
                    continue
                new_terms = matched - best_matched
                if new_terms or sc >= best_score * 0.5:
                    second = st
                    break
    if second is not None:
        a, b = sorted((best, second))
        wa = win if a == best else (limit - win - 20)
        wb = (limit - win - 20) if a == best else win
        p1 = ("[...] " if a > 0 else "") + text[a : a + wa]
        p2 = text[b : b + wb] + (" [...]" if b + wb < len(text) else "")
        return (
            f"{p1} [...] {p2} [2 excerpts of {len(text)} chars at {a},{b}; read more with"
            f" substr(content, n, 4000) or api.context]"
        ), True
    cut = text[best : best + limit]
    prefix = "[...] " if best > 0 else ""
    suffix = " [...]" if best + limit < len(text) else ""
    return (
        f"{prefix}{cut}{suffix} [excerpt of {len(text)} chars at {best}; read more with"
        f" substr(content, n, 4000) or api.context]"
    ), True




# --- recall responses -----------------------------------------------------------

RECALL_EXCERPT = 1200
RECALL_IMAGES = 3
RECALL_IMAGE_OCR = 120


def _short_image(image):
    if not isinstance(image, dict):
        return image
    ocr = image.get("ocr")
    if isinstance(ocr, str) and len(ocr) > RECALL_IMAGE_OCR:
        image = dict(image, ocr=ocr[: RECALL_IMAGE_OCR - 3] + "...")
    return image


def _size(value):
    return len(json.dumps(value, default=str))


def shrink_recall(result, query, max_chars):
    """A recall response dict cut to about max_chars for a reader: coverage
    summarized, dropped listed by count, long contents excerpted around the
    query, then trailing records left out. Returns (result, changed)."""
    if _size(result) <= max_chars:
        return result, False
    result = dict(result)
    notes = []
    execution = dict(result.get("execution") or {})
    coverage = execution.get("coverage")
    if isinstance(coverage, dict) and isinstance(coverage.get("channels"), list):
        channels = coverage["channels"]
        execution["coverage"] = dict(
            {k: v for k, v in coverage.items() if k != "channels"},
            channels_checked=len(channels),
            not_complete=sorted(
                {c.get("channel") for c in channels
                 if isinstance(c, dict) and c.get("status") not in ("complete", "covered")}
                - {None}
            )[:60],
        )
    dropped = result.get("dropped")
    if isinstance(dropped, list) and dropped:
        result["dropped"] = dict(count=len(dropped))
    records = []
    excerpted = 0
    for record in result.get("records") or []:
        content = record.get("content") if isinstance(record, dict) else None
        if isinstance(content, str) and len(content) > RECALL_EXCERPT:
            record = dict(record, content=snippet(content, query, RECALL_EXCERPT)[0])
            excerpted += 1
        records.append(record)
    if excerpted:
        notes.append(
            f"{excerpted} long contents excerpted around the query; read a record whole"
            " with SQL (api.messages by id) or pass view=full"
        )
    result["records"] = records
    result["execution"] = execution
    if _size(result) > max_chars:
        trimmed = 0
        for index, record in enumerate(records):
            images = record.get("images") if isinstance(record, dict) else None
            if not isinstance(images, list) or not images:
                continue
            short = [_short_image(i) for i in images[:RECALL_IMAGES]]
            if short != images:
                records[index] = dict(record, images=short)
                trimmed += 1
        if trimmed:
            notes.append(
                f"images of {trimmed} records shortened (at most {RECALL_IMAGES} per record,"
                " OCR cut); GET /v1/assets/{asset_id} has the rest"
            )
    while len(result["records"]) > 1 and _size(result) > max_chars:
        result["records"] = result["records"][:-1]
    left = len(records) - len(result["records"])
    if left:
        notes.append(
            f"{left} lower-ranked records left out at max_chars={max_chars}; lower k,"
            " or use POST /v1/find for compact episodes"
        )
    execution["warnings"] = list(execution.get("warnings") or []) + notes
    return result, True


# --- times for readers ------------------------------------------------------------


def owner_zone():
    """THROUGHLINE_TIMEZONE (an IANA zone such as Europe/Lisbon), else the
    deprecated THROUGHLINE_OWNER_TZ, else UTC: the same setting people,
    cadence and api.day read, so one variable sets the owner's zone
    everywhere (throughline.tz)."""
    from . import tz

    return tz.zone()


def local_time(value):
    """A record time as the owner's local time with weekday, e.g.
    '2024-03-15 Fri 23:05 -05:00'; None stays None."""
    from datetime import datetime

    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value[:16]
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        from datetime import timezone

        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(owner_zone())
    offset = local.strftime("%z")
    return local.strftime("%Y-%m-%d %a %H:%M ") + offset[:3] + ":" + offset[3:]
