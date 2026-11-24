"""Loopback HTTP relay for contract evidence and deterministic crash barriers."""

import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx


@contextlib.contextmanager
def capture(url, *, stop_at=None):
    evidence = []
    blocked = threading.Event()
    release = threading.Event()
    upstream = httpx.Client(timeout=60)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def forward(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            response = upstream.request(
                self.command,
                url + self.path,
                content=body,
                headers={
                    "Authorization": self.headers.get("Authorization", ""),
                    "Content-Type": "application/json",
                },
            )
            evidence.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "body": json.loads(body) if body else None,
                    "status": response.status_code,
                    "response": response.text,
                }
            )
            # Stop after the core committed but before the plugin gets its ACK.
            # A crash here exercises safe replay of an ambiguous write.
            if stop_at is not None and len(evidence) == stop_at:
                blocked.set()
                release.wait(30)
            try:
                self.send_response(response.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        do_GET = do_POST = do_PUT = do_PATCH = forward

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", evidence, blocked
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        upstream.close()
