"""Bridge capabilities: what this desktop will do for a connector elsewhere.

Every capability is off until the owner switches it on in the companion and
names the plugins allowed to use it; the server checks the same allow-list
before a request reaches this computer, and the companion checks it again
here. Each one is narrow on purpose:

  cookies        cookies of one allow-listed site from a local browser, as a
                 Cookie header or a Netscape cookies.txt
  file.read      one file below an allow-listed folder (size-capped)
  http.local     one request to a named localhost service (Beeper Desktop,
                 LM Studio, ActivityWatch); never an arbitrary URL
  plugin.run     run one of this desktop's connectors now
  status         what this companion runs and offers (no data)
"""

import base64
import os
import time
from pathlib import Path

import httpx

from . import paths

MAX_FILE = 900_000
MAX_HTTP = 900_000


class Refused(Exception):
    """A request the capability's own rules do not allow."""


def _domain_allowed(domain, allowed):
    domain = domain.lower().lstrip(".")
    for item in allowed:
        item = item.lower().lstrip(".")
        if domain == item or domain.endswith("." + item):
            return True
    return False


def cookies(options, args):
    """Cookies for args.domain from args.browser (default: the first allowed
    browser that has any). Answer: {header, text (Netscape), count, browser}."""
    domain = str(args.get("domain") or "").strip().lower()
    if not domain:
        raise Refused("domain is required")
    if not _domain_allowed(domain, options.get("domains") or []):
        raise Refused(f"cookies for {domain} are not allowed on this desktop")
    browsers = options.get("browsers") or []
    wanted = args.get("browser")
    if wanted and wanted not in browsers:
        raise Refused(f"browser {wanted} is not allowed on this desktop")
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except ImportError:
        raise Refused("cookie extraction needs yt-dlp (install the cookies extra)") from None
    errors = []
    for browser in [wanted] if wanted else browsers:
        try:
            jar = extract_cookies_from_browser(browser, profile=args.get("profile"))
        except Exception as error:  # noqa: BLE001 - browser missing or locked
            errors.append(f"{browser}: {type(error).__name__}")
            continue
        picked = [
            c for c in jar if _domain_allowed(c.domain, [domain]) or _domain_allowed(domain, [c.domain])
        ]
        if not picked:
            errors.append(f"{browser}: no cookies for {domain}")
            continue
        header = "; ".join(f"{c.name}={c.value}" for c in picked)
        lines = ["# Netscape HTTP Cookie File"]
        for c in picked:
            lines.append(
                "\t".join(
                    [
                        c.domain,
                        "TRUE" if c.domain.startswith(".") else "FALSE",
                        c.path or "/",
                        "TRUE" if c.secure else "FALSE",
                        str(int(c.expires or 0)),
                        c.name,
                        c.value or "",
                    ]
                )
            )
        return dict(header=header, text="\n".join(lines) + "\n", count=len(picked), browser=browser)
    raise Refused("no cookies found (" + "; ".join(errors) + ")")


def _inside(path, roots):
    real = Path(os.path.realpath(path))
    for root in roots:
        base = Path(os.path.realpath(os.path.expanduser(root)))
        if real == base or base in real.parents:
            return real
    return None


def file_read(options, args):
    """One file below an allowed folder. Answer: {path, size, mtime, text or
    base64}."""
    name = args.get("path")
    if not name:
        raise Refused("path is required")
    real = _inside(os.path.expanduser(str(name)), options.get("roots") or [])
    if real is None:
        raise Refused("that file is outside the folders this desktop shares")
    if not real.is_file():
        raise Refused("no such file")
    size = real.stat().st_size
    if size > MAX_FILE:
        raise Refused(f"file is larger than {MAX_FILE} bytes")
    data = real.read_bytes()
    answer = dict(path=str(real), size=size, mtime=real.stat().st_mtime)
    try:
        answer["text"] = data.decode("utf-8")
    except UnicodeDecodeError:
        answer["base64"] = base64.b64encode(data).decode()
    return answer


def file_list(options, args):
    """Files directly below an allowed folder (names, sizes, times)."""
    real = _inside(os.path.expanduser(str(args.get("path") or "")), options.get("roots") or [])
    if real is None or not real.is_dir():
        raise Refused("that folder is not shared by this desktop")
    items = []
    for item in sorted(real.iterdir())[:2000]:
        try:
            stat = item.stat()
        except OSError:
            continue
        items.append(dict(name=item.name, dir=item.is_dir(), size=stat.st_size, mtime=stat.st_mtime))
    return dict(path=str(real), items=items)


def http_local(options, args):
    """One request to a named local service. args: {target, method, path,
    query, json, headers}. Answer: {status, headers, json or text}."""
    targets = options.get("targets") or {}
    target = args.get("target")
    if target not in targets:
        raise Refused(f"no local service named {target} is shared")
    base = str(targets[target]).rstrip("/")
    host = httpx.URL(base).host
    if host not in ("localhost", "127.0.0.1", "::1"):
        raise Refused("only localhost services can be shared")
    method = str(args.get("method") or "GET").upper()
    if method not in ("GET", "POST", "PUT"):
        raise Refused("method not allowed")
    path = "/" + str(args.get("path") or "").lstrip("/")
    if ".." in path.split("/"):
        raise Refused("path not allowed")
    headers = {k: str(v) for k, v in (args.get("headers") or {}).items()}
    response = httpx.request(
        method,
        base + path,
        params=args.get("query") or None,
        json=args.get("json"),
        headers=headers,
        timeout=30,
    )
    body = response.content[:MAX_HTTP]
    answer = dict(
        status=response.status_code,
        headers={k: v for k, v in response.headers.items() if k.lower() in ("content-type", "etag", "last-modified")},
    )
    if "json" in response.headers.get("content-type", ""):
        try:
            answer["json"] = response.json()
            return answer
        except ValueError:
            pass
    answer["text"] = body.decode("utf-8", errors="replace")
    return answer


class Providers:
    """Dispatch bridge requests to capabilities, applying the local settings."""

    def __init__(self, config, runner=None, status=None):
        self.config = config
        self.runner = runner
        self.status = status

    def declared(self):
        """Capabilities to announce: enabled ones with their allow-lists."""
        out = []
        for name, options in sorted(self.config.data["bridge"]["capabilities"].items()):
            if not options.get("enabled"):
                continue
            out.append(
                dict(
                    name=name,
                    description=DESCRIPTIONS.get(name, ""),
                    allowed_plugins=list(options.get("allowed_plugins") or []),
                )
            )
        if any(c["name"] == "file.read" for c in out):
            options = self.config.capability("file.read")
            out.append(dict(name="file.list", description=DESCRIPTIONS["file.list"],
                            allowed_plugins=list(options.get("allowed_plugins") or [])))
        return out

    def handle(self, request):
        name = request["capability"]
        lookup = "file.read" if name == "file.list" else name
        options = self.config.capability(lookup)
        if not options or not options.get("enabled"):
            raise Refused(f"{name} is switched off on this desktop")
        allowed = options.get("allowed_plugins") or []
        plugin = request.get("plugin")
        if plugin is not None and "*" not in allowed and plugin not in allowed:
            raise Refused(f"{plugin} may not use {name} on this desktop")
        args = request.get("args") or {}
        if name == "cookies":
            return cookies(options, args)
        if name == "file.read":
            return file_read(options, args)
        if name == "file.list":
            return file_list(options, args)
        if name == "http.local":
            return http_local(options, args)
        if name == "plugin.run":
            if not self.runner:
                raise Refused("no runner")
            try:
                return self.runner.run_now(str(args.get("plugin") or ""))
            except KeyError:
                raise Refused("that connector does not run on this desktop") from None
        if name == "status":
            return self.status() if self.status else dict(time=time.time())
        raise Refused(f"unknown capability {name}")


DESCRIPTIONS = {
    "cookies": "Browser cookies of allow-listed sites",
    "file.read": "Files below shared folders",
    "file.list": "Folder listings below shared folders",
    "http.local": "Named localhost services",
    "plugin.run": "Run one of this desktop's connectors now",
    "status": "What this companion runs and offers",
}


def platform_name():
    return paths.platform()
