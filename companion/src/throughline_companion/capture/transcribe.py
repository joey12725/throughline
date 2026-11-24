"""Speech to text. Real transcription only (no meeting-note services).

Engines (capture.transcription.engine):
  openai   any OpenAI-compatible /audio/transcriptions endpoint. Presets:
           openai  https://api.openai.com/v1      gpt-4o-mini-transcribe
           groq    https://api.groq.com/openai/v1  whisper-large-v3-turbo
           custom  base_url you give (a local speaches / whisper.cpp server,
                   Together, Fireworks, ...)
  deepgram https://api.deepgram.com/v1/listen, model nova-3
  local    on-device faster-whisper; the runtime and the model are
           downloaded only when you ask (local_whisper.py)

Each returns {text, language, segments:[{start, end, text}], engine, model}.
"""

import json

import httpx

PRESETS = {
    "openai": dict(base_url="https://api.openai.com/v1", model="gpt-4o-mini-transcribe"),
    "groq": dict(base_url="https://api.groq.com/openai/v1", model="whisper-large-v3-turbo"),
    "custom": dict(base_url="http://localhost:8000/v1", model="whisper-1"),
    "deepgram": dict(base_url="https://api.deepgram.com/v1", model="nova-3"),
    "local": dict(base_url="", model="small"),
}


class TranscriptionError(RuntimeError):
    pass


def settings_of(options):
    engine = options.get("engine") or "groq"
    if engine not in PRESETS:
        raise TranscriptionError(f"unknown transcription engine {engine}")
    preset = PRESETS[engine]
    return dict(
        engine=engine,
        base_url=(options.get("base_url") or preset["base_url"]).rstrip("/"),
        model=options.get("model") or preset["model"],
        api_key=options.get("api_key") or "",
        language=options.get("language") or None,
        prompt=options.get("prompt") or None,
    )


def transcribe(path, options, *, local=None):
    s = settings_of(options)
    if s["engine"] == "local":
        if local is None:
            raise TranscriptionError("local transcription is not installed")
        return local.transcribe(path, s["language"])
    if s["engine"] == "deepgram":
        return _deepgram(path, s)
    return _openai(path, s)


def _openai(path, s):
    if s["engine"] in ("openai", "groq") and not s["api_key"]:
        raise TranscriptionError(f"{s['engine']} needs an API key")
    verbose = not s["model"].startswith("gpt-4o")
    data = {"model": s["model"], "response_format": "verbose_json" if verbose else "json"}
    if s["language"]:
        data["language"] = s["language"]
    if s["prompt"]:
        data["prompt"] = s["prompt"]
    headers = {"Authorization": "Bearer " + s["api_key"]} if s["api_key"] else {}
    with open(path, "rb") as handle:
        response = httpx.post(
            s["base_url"] + "/audio/transcriptions",
            data=data,
            files={"file": (path.name if hasattr(path, "name") else "audio.wav", handle, "audio/wav")},
            headers=headers,
            timeout=120,
        )
    if response.status_code != 200:
        raise TranscriptionError(f"{s['engine']} answered {response.status_code}: {response.text[:200]}")
    body = response.json()
    segments = [
        dict(start=float(x.get("start") or 0), end=float(x.get("end") or 0), text=(x.get("text") or "").strip())
        for x in body.get("segments") or []
    ]
    return dict(text=(body.get("text") or "").strip(), language=body.get("language"),
                segments=segments, engine=s["engine"], model=s["model"])


def _deepgram(path, s):
    if not s["api_key"]:
        raise TranscriptionError("deepgram needs an API key")
    params = {"model": s["model"], "smart_format": "true", "utterances": "true"}
    if s["language"]:
        params["language"] = s["language"]
    else:
        params["detect_language"] = "true"
    with open(path, "rb") as handle:
        response = httpx.post(
            s["base_url"] + "/listen",
            params=params,
            content=handle.read(),
            headers={"Authorization": "Token " + s["api_key"], "Content-Type": "audio/wav"},
            timeout=120,
        )
    if response.status_code != 200:
        raise TranscriptionError(f"deepgram answered {response.status_code}: {response.text[:200]}")
    body = response.json()
    channel = ((body.get("results") or {}).get("channels") or [{}])[0]
    alt = (channel.get("alternatives") or [{}])[0]
    segments = [
        dict(start=float(u.get("start") or 0), end=float(u.get("end") or 0), text=(u.get("transcript") or "").strip())
        for u in (body.get("results") or {}).get("utterances") or []
    ]
    return dict(text=(alt.get("transcript") or "").strip(), language=channel.get("detected_language"),
                segments=segments, engine="deepgram", model=s["model"])


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
