#!/usr/bin/env python3
"""Gmail attachment bytes through Composio, standard library only (Python 3.12+).

Runs on the host that holds the Composio key (secrets never leave it):

    python email_fetch.py --requests requests.json --out DIR \
        [--account-map mailbox=connected_account_id ...] [--max N] [--min-interval S]

requests.json is the list `throughline lib images email-requests` writes:
[{"account": "<mailbox>", "message_id", "attachment_id", "file_name", "mime_type"}].
Each attachment is fetched with the Composio tool GMAIL_GET_ATTACHMENT
(POST {COMPOSIO_BASE_URL}/api/v3/tools/execute/GMAIL_GET_ATTACHMENT with
COMPOSIO_API_KEY and the mailbox's connected account id) and written to
DIR/files/<sha256>.<ext>; DIR/index.jsonl gets one line per request:
{"message_id", "attachment_id", "account", "file": "files/<sha>.<ext>",
 "sha256", "bytes", "mime_type"} or {..., "error": "..."}.
Already-indexed requests are skipped, so a stopped run resumes. Copy DIR back
and point IMAGES_EMAIL_IMPORT_DIR at it.

Connected account ids: --account-map, else EMAIL_ACCOUNT_MAP
("mailbox=id,mailbox=id"), else EMAIL_ACCOUNTS when it names one account.
Composio answers either inline base64 (data / content / file.content) or a
file download url (s3url / file.s3url / url); both are handled.
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TOOL = "GMAIL_GET_ATTACHMENT"
DEFAULT_BASE_URL = "https://backend.composio.dev"
EXT = {
    "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp",
    "image/heic": "heic", "image/heif": "heif", "image/avif": "avif", "image/bmp": "bmp",
    "image/tiff": "tiff",
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _post(url, body, key, retries=4):
    data = json.dumps(body).encode()
    opener = urllib.request.build_opener(_NoRedirect)
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "x-api-key": key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "throughline-images-email-fetch/1",
            },
        )
        try:
            with opener.open(request, timeout=120) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            if error.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(min(2**attempt * 2, 60))
                continue
            raise RuntimeError(f"Composio HTTP {error.code}") from None
        except urllib.error.URLError as error:
            if attempt < retries:
                time.sleep(min(2**attempt * 2, 60))
                continue
            raise RuntimeError(f"Composio transport: {type(error).__name__}") from None
    raise RuntimeError("unreachable")


def _download(url):
    # A presigned object url: no key header is sent.
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def _find(value, *names):
    if isinstance(value, dict):
        for name in names:
            if isinstance(value.get(name), str) and value[name]:
                return value[name]
        for inner in value.values():
            found = _find(inner, *names)
            if found:
                return found
    elif isinstance(value, list):
        for inner in value:
            found = _find(inner, *names)
            if found:
                return found
    return None


def attachment_bytes(answer):
    if answer.get("successful") is False or answer.get("error"):
        message = answer.get("error")
        if isinstance(message, dict):
            message = message.get("message")
        raise RuntimeError(f"{TOOL} failed: {str(message or 'unsuccessful')[:200]}")
    data = answer.get("data") or {}
    url = _find(data, "s3url", "s3_url", "download_url", "url")
    if url and url.startswith("http"):
        return _download(url)
    inline = _find(data, "data", "content", "base64", "file_content")
    if inline:
        inline = inline.replace("-", "+").replace("_", "/")
        return base64.b64decode(inline + "=" * (-len(inline) % 4))
    raise RuntimeError(f"{TOOL} returned no file")


def account_map(pairs, env):
    out = {}
    for pair in list(pairs or []) + [p for p in (env.get("EMAIL_ACCOUNT_MAP") or "").split(",") if p]:
        if "=" in pair:
            mailbox, ref = pair.split("=", 1)
            out[mailbox.strip().lower()] = ref.strip()
    single = [a for a in (env.get("EMAIL_ACCOUNTS") or "").split(",") if a.strip()]
    return out, (single[0].strip() if len(single) == 1 else None)


def run(args, env=None):
    env = os.environ if env is None else env
    key = env.get("COMPOSIO_API_KEY")
    if not key:
        raise SystemExit("COMPOSIO_API_KEY is required")
    base = (env.get("COMPOSIO_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    mapping, fallback = account_map(args.account_map, env)
    requests = json.loads(Path(args.requests).read_text(encoding="utf-8"))
    out = Path(args.out)
    (out / "files").mkdir(parents=True, exist_ok=True)
    index_path = out / "index.jsonl"
    done = set()
    if index_path.exists():
        for line in index_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not row.get("error"):
                done.add((row.get("message_id"), row.get("attachment_id")))
    counts = {"ok": 0, "error": 0, "skipped": 0}
    with index_path.open("a", encoding="utf-8") as index:
        for n, req in enumerate(requests):
            if args.max and counts["ok"] + counts["error"] >= args.max:
                break
            ident = (req["message_id"], req["attachment_id"])
            if ident in done:
                counts["skipped"] += 1
                continue
            ref = mapping.get((req.get("account") or "").lower()) or fallback
            row = {"message_id": req["message_id"], "attachment_id": req["attachment_id"], "account": req.get("account")}
            try:
                if not ref:
                    raise RuntimeError("no connected account id for this mailbox")
                body = {
                    "connected_account_id": ref,
                    "arguments": {
                        "message_id": req["message_id"],
                        "attachment_id": req["attachment_id"],
                        "file_name": req.get("file_name") or "attachment",
                        "user_id": "me",
                    },
                }
                if env.get("COMPOSIO_USER_ID"):
                    body["user_id"] = env["COMPOSIO_USER_ID"]
                data = attachment_bytes(_post(f"{base}/api/v3/tools/execute/{TOOL}", body, key))
                sha = hashlib.sha256(data).hexdigest()
                ext = EXT.get((req.get("mime_type") or "").lower()) or "bin"
                name = f"files/{sha}.{ext}"
                (out / name).write_bytes(data)
                row.update(file=name, sha256=sha, bytes=len(data), mime_type=req.get("mime_type"))
                counts["ok"] += 1
            except Exception as error:  # One attachment's failure costs only itself.
                row["error"] = str(error)[:300]
                counts["error"] += 1
            index.write(json.dumps(row, sort_keys=True) + "\n")
            index.flush()
            if args.min_interval:
                time.sleep(args.min_interval)
    print(json.dumps(counts))
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--requests", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--account-map", action="append", default=[])
    parser.add_argument("--max", type=int, default=0)
    parser.add_argument("--min-interval", type=float, default=0.5)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(0 if main() is not None else 1)
