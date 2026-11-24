"""Zero-shot kind and tags from a SigLIP image vector, plus cheap signals.

    vocab = load_vocabulary()
    bank = TextBank(ml, vocab, cache_path)       # prompt vectors (embedded once, cached)
    sims = bank.similarities(image_vector)       # {'kinds': {kind: cos}, 'tags': {tag: cos}}
    result = classify(sims, signals_of(asset, ocr_boxes, faces, name), vocab)

classify() is pure: kind = argmax over prompt-ensemble similarities (max per
kind, times kind_temperature) plus log-odds priors from the signals (camera
EXIF, file name, screen-shaped resolution, OCR density, sticker shape, mime).
A tag's margin is its best prompt's cosine minus the cosine of that prompt's
template reference ("a photo of something" for "a photo of ..."), which
removes the template's own pull; prob = sigmoid(logit_scale*margin +
logit_bias), and a tag is kept when prob reaches its threshold and the tag
applies to the chosen kind. `meme` adds a caption-overlay bonus (OCR text in
the top or bottom band of a non-document image) and source hints. A few tags
come from signals alone: text-heavy (OCR), group-photo / selfie (faces), nsfw.
"""

import hashlib
import json
import math
import re
from pathlib import Path

VOCABULARY = Path(__file__).with_name("tags.json")
KINDS = ("photo", "screenshot", "document", "artwork", "video", "other")


def load_vocabulary(path=None):
    vocab = json.loads(Path(path or VOCABULARY).read_text(encoding="utf-8"))
    for kind in vocab["kinds"]:
        if kind not in KINDS:
            raise ValueError(f"unknown kind in vocabulary: {kind}")
    return vocab


def vocabulary_hash(vocab):
    return hashlib.sha256(json.dumps(vocab, sort_keys=True).encode()).hexdigest()[:12]


def _norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def sigmoid(x):
    if x < -60:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


class TextBank:
    """Prompt vectors from the Immich ML text tower, cached on disk per model."""

    def __init__(self, ml, vocab, cache_path=None):
        self.vocab = vocab
        self.cache_path = Path(cache_path) if cache_path else None
        cache = {}
        if self.cache_path and self.cache_path.exists():
            try:
                stored = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if stored.get("model") == getattr(ml, "model", None):
                    cache = stored.get("prompts", {})
            except ValueError:
                cache = {}
        prompts = {p for ps in vocab["kinds"].values() for p in ps}
        prompts |= {p for t in vocab["tags"].values() for p in t["prompts"]}
        prompts |= set(vocab.get("references", {}).values())
        missing = sorted(prompts - set(cache))
        for prompt in missing:
            cache[prompt] = _norm(ml.embed_text(prompt))
        if missing and self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps({"model": getattr(ml, "model", None), "prompts": cache}), encoding="utf-8"
            )
        self.vectors = cache

    def similarities(self, image_vector):
        """{'kinds': {kind: best cosine}, 'tags': {tag: best margin over its reference}}."""
        image = _norm(image_vector)
        sim = {p: _dot(image, v) for p, v in self.vectors.items()}
        return similarities_from(sim, self.vocab)


def reference_of(prompt, vocab):
    refs = vocab.get("references", {})
    best = max((prefix for prefix in refs if prompt.startswith(prefix)), key=len, default=None)
    return refs.get(best) if best is not None else None


def similarities_from(sim, vocab):
    """Kind cosines and tag margins from {prompt: cosine}."""

    def margin(prompt):
        ref = reference_of(prompt, vocab)
        return sim[prompt] - (sim.get(ref, 0.0) if ref else 0.0)

    return {
        "kinds": {k: max(sim[p] for p in ps) for k, ps in vocab["kinds"].items()},
        "tags": {t: max(margin(p) for p in e["prompts"]) for t, e in vocab["tags"].items()},
        # Best raw cosine per tag, for the library-relative rule (vocab "medians").
        "tag_raw": {t: max(sim[p] for p in e["prompts"]) for t, e in vocab["tags"].items()},
    }


SCREENSHOT_NAME = re.compile(r"(screen ?shot|screenshot|scrnli|capture|bildschirmfoto|snip|screen_recording)", re.I)
CAMERA_NAME = re.compile(r"^(pxl_|img_\d|dsc|dscn|dcim|mvimg|photo_|\d{8}_\d{6}|signal-\d{4}|snap-)", re.I)
PHONE_WIDTHS = {640, 720, 750, 828, 1080, 1125, 1170, 1179, 1242, 1284, 1290, 1440, 1536, 1668, 2048}
DESKTOP = {
    (1280, 720), (1280, 800), (1366, 768), (1440, 900), (1536, 864), (1600, 900), (1680, 1050),
    (1920, 1080), (1920, 1200), (2048, 1152), (2560, 1080), (2560, 1440), (2560, 1600),
    (2880, 1800), (3024, 1964), (3456, 2234), (3440, 1440), (3840, 2160), (5120, 2880),
}


def screen_shaped(width, height):
    if not width or not height:
        return False
    w, h = sorted((width, height))
    if (h, w) in DESKTOP:
        return True
    return w in PHONE_WIDTHS and h / w >= 1.6


def caption_overlay(boxes, width=None, height=None):
    """True when OCR text sits in the top or bottom fifth, as image-macro captions do.

    `boxes` are OCR boxes with normalized (0..1) y1..y4, or pixel coordinates
    with the image height given."""
    top = bottom = 0
    middle = 0
    for b in boxes or []:
        ys = [b.get(k) for k in ("y1", "y2", "y3", "y4") if b.get(k) is not None]
        if not ys or not (b.get("text") or "").strip():
            continue
        if height and max(ys) > 1.5:
            ys = [y / height for y in ys]
        cy = sum(ys) / len(ys)
        if cy <= 0.22:
            top += 1
        elif cy >= 0.78:
            bottom += 1
        else:
            middle += 1
    return (top + bottom) >= 1 and middle <= max(2, top + bottom)


def signals_of(*, mime=None, width=None, height=None, name=None, exif=None, ocr_text=None,
               ocr_boxes=None, faces=None, nsfw=None, origin=None, source_hints=(), nsfw_threshold=0.6):
    exif = exif or {}
    area = (width or 0) * (height or 0)
    face_areas = []
    for f in faces or []:
        box = f.get("bbox") or {}
        if area and box.get("w") and box.get("h"):
            face_areas.append(box["w"] * box["h"] / area)
    return {
        "mime": (mime or "").lower(),
        "width": width,
        "height": height,
        "name": name or "",
        "camera": bool(exif.get("make") or exif.get("model")),
        "ocr_chars": len((ocr_text or "").strip()),
        "ocr_boxes": len(ocr_boxes or []),
        "caption_overlay": caption_overlay(ocr_boxes, width, height),
        "faces": len(faces or []),
        "largest_face": max(face_areas, default=0.0),
        "nsfw": nsfw,
        "nsfw_threshold": nsfw_threshold,
        "origin": origin,
        "hints": " ".join(str(h) for h in source_hints if h).lower(),
    }


def kind_priors(signals):
    prior = {k: 0.0 for k in KINDS if k != "video"}
    prior["other"] -= 1.0  # the rare kind: chosen only on clear evidence
    mime, name = signals["mime"], signals["name"]
    png_like = mime in ("image/png", "image/webp", "image/gif", "image/bmp")
    if signals["camera"]:
        prior["photo"] += 4.0
        prior["screenshot"] -= 2.0
        prior["artwork"] -= 1.5
    if SCREENSHOT_NAME.search(name):
        prior["screenshot"] += 4.0
    elif CAMERA_NAME.search(name) and mime in ("image/jpeg", "image/heic", "image/heif"):
        prior["photo"] += 1.5
    if mime in ("image/jpeg", "image/heic", "image/heif") and not screen_shaped(signals["width"], signals["height"]):
        prior["photo"] += 0.5
    if screen_shaped(signals["width"], signals["height"]) and not signals["camera"]:
        prior["screenshot"] += 2.0 if png_like else 1.0
    if png_like and not signals["camera"]:
        prior["photo"] -= 0.5
        prior["screenshot"] += 0.5
        prior["artwork"] += 0.3
    chars = signals["ocr_chars"]
    if chars >= 200:
        prior["screenshot"] += 1.0
        prior["document"] += 1.0
        prior["photo"] -= 1.0
    elif chars == 0 and signals["ocr_boxes"] == 0:
        prior["document"] -= 1.5
        prior["screenshot"] -= 0.5
    w, h = signals["width"] or 0, signals["height"] or 0
    if mime in ("image/webp", "image/png", "image/gif") and w and h and max(w, h) <= 640 and abs(w - h) <= 0.1 * max(w, h):
        prior["artwork"] += 1.5  # sticker / emoji shaped
    return prior


def classify(sims, signals, vocab):
    """{'kind', 'kind_scores', 'tags', 'tag_scores'} — pure."""
    if signals["mime"].startswith("video/"):
        return {"kind": "video", "kind_scores": {"video": 1.0}, "tags": [], "tag_scores": {}}
    temperature = vocab.get("kind_temperature", 100.0)
    prior = kind_priors(signals)
    logits = {k: temperature * sims["kinds"].get(k, 0.0) + prior.get(k, 0.0) for k in vocab["kinds"]}
    top = max(logits.values())
    exp = {k: math.exp(v - top) for k, v in logits.items()}
    total = sum(exp.values())
    kind_scores = {k: round(v / total, 4) for k, v in exp.items()}
    kind = max(kind_scores, key=kind_scores.get)

    scale, bias = vocab["logit_scale"], vocab["logit_bias"]
    default = vocab.get("default_threshold", 0.5)
    probs = {t: sigmoid(scale * s + bias) for t, s in sims["tags"].items()}
    chosen = {}
    medians = vocab.get("medians")
    if medians and sims.get("tag_raw"):
        # Library-relative rule (calibrated 2026-11-22 on 5,208 images by eye):
        # a tag fires when its best raw cosine beats that tag's library median
        # by min_delta AND it is among the image's top `top_k` tags by that lift.
        # The absolute sigmoid scores saturate and fire on almost everything.
        rule = vocab.get("relative", {})
        min_delta, top_k = rule.get("min_delta", 0.085), rule.get("top_k", 2)
        lift = {t: c - medians[t] for t, c in sims["tag_raw"].items() if t in medians and t != "meme"}
        ranked = sorted(lift, key=lambda t: -lift[t])[:top_k]
        for tag in ranked:
            entry = vocab["tags"][tag]
            if entry.get("kinds") and kind not in entry["kinds"]:
                continue
            if lift[tag] >= entry.get("min_delta", min_delta):
                chosen[tag] = round(min(1.0, 0.5 + 5 * lift[tag]), 4)
    else:
        for tag, prob in probs.items():
            entry = vocab["tags"][tag]
            if tag == "meme":
                continue
            if entry.get("kinds") and kind not in entry["kinds"]:
                continue
            if prob >= entry.get("threshold", default):
                chosen[tag] = prob
    meme_cfg = vocab.get("meme", {})
    meme = probs.get("meme", 0.0)
    if signals["caption_overlay"] and kind in ("photo", "artwork", "other", "screenshot"):
        meme += meme_cfg.get("caption_bonus", 0.25)
    if any(h in signals["hints"] or h in signals["name"].lower() for h in meme_cfg.get("hints", [])):
        meme += meme_cfg.get("hint_bonus", 0.15)
    if kind == "document":
        meme -= 0.2
    if meme >= meme_cfg.get("threshold", 0.5):
        chosen["meme"] = min(meme, 1.0)
    limit = vocab.get("max_tags", 8)
    tags = sorted(chosen, key=lambda t: -chosen[t])[:limit]
    if signals["ocr_chars"] >= 300:
        tags.append("text-heavy")
    if kind == "photo" and signals["faces"] >= 3:
        tags.append("group-photo")
    if kind == "photo" and signals["faces"] == 1 and signals["largest_face"] >= 0.08:
        tags.append("selfie")
    if signals.get("nsfw") is not None and signals["nsfw"] >= signals.get("nsfw_threshold", 0.6):
        tags.append("nsfw")
    seen = []
    for t in tags:
        if t not in seen:
            seen.append(t)
    return {
        "kind": kind,
        "kind_scores": kind_scores,
        "tags": seen,
        "tag_scores": {t: round(p, 4) for t, p in sorted(probs.items(), key=lambda x: -x[1])[:15]},
    }
