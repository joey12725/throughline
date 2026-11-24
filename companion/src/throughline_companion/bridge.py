"""The desktop side of the bridge: say hello, long-poll, answer.

Only outbound HTTPS to the Throughline server is used, so the desktop needs no
open port and works behind NAT, VPNs and reverse proxies. Hello is repeated
every minute with the capabilities, allow-lists and connector states, so the
server always sees what the owner currently allows.
"""

import base64
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import __version__, paths
from .providers import Refused

log = logging.getLogger("throughline_companion.bridge")

PAIRING_PREFIX = "tlc1."
HELLO_EVERY_S = 60


def decode_pairing(code):
    code = (code or "").strip()
    if not code.startswith(PAIRING_PREFIX):
        raise ValueError("not a Throughline companion pairing code (it starts with tlc1.)")
    body = code[len(PAIRING_PREFIX):]
    body += "=" * (-len(body) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(body))
    except ValueError:
        raise ValueError("the pairing code is damaged; copy it again") from None


class Bridge:
    def __init__(self, config, providers, *, plugins=None):
        self.config = config
        self.providers = providers
        self.plugins = plugins or (lambda: [])
        self.stopping = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bridge")
        self.state = "stopped"
        self.last_error = None
        self.last_contact = None
        self.served = 0
        self.thread = None
        self.active = None
        # Seconds the server holds each long-poll open.
        self.poll_wait = 25

    def snapshot(self):
        return dict(
            state=self.state,
            last_error=self.last_error,
            last_contact=self.last_contact,
            served=self.served,
            capabilities=self.providers.declared(),
        )

    def _client(self):
        server = self.config.server
        return httpx.Client(
            base_url=server["url"].rstrip("/") + "/v1/",
            headers={"Authorization": "Bearer " + server["device_token"]},
            timeout=httpx.Timeout(45, connect=10),
        )

    def hello(self, client):
        response = client.post(
            "companion/hello",
            json=dict(
                platform=paths.platform(),
                app_version=__version__,
                capabilities=self.providers.declared(),
                plugins=self.plugins(),
            ),
        )
        response.raise_for_status()
        return response.json()

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="bridge", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
        # Abort a long-poll in flight so shutdown is immediate.
        client = self.active
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _loop(self):
        delay = 5
        while not self.stopping.is_set():
            if not (self.config.paired and self.config.data["bridge"].get("enabled", True)):
                self.state = "off" if self.config.paired else "not_paired"
                self.stopping.wait(5)
                continue
            try:
                with self._client() as client:
                    self.active = client
                    self.hello(client)
                    self.state = "connected"
                    self.last_error = None
                    delay = 5
                    said_hello = time.monotonic()
                    while not self.stopping.is_set():
                        if time.monotonic() - said_hello > HELLO_EVERY_S:
                            self.hello(client)
                            said_hello = time.monotonic()
                        response = client.get("companion/poll", params={"wait": self.poll_wait})
                        response.raise_for_status()
                        self.last_contact = time.time()
                        for request in response.json().get("requests") or []:
                            self.pool.submit(self._serve, request)
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                self.state = "refused" if status in (401, 403) else "error"
                self.last_error = f"server answered {status}"
                if status in (401, 403):
                    delay = 300
            except (httpx.TransportError, ValueError, RuntimeError) as error:
                if self.stopping.is_set():
                    break
                self.state = "offline"
                self.last_error = type(error).__name__
            finally:
                self.active = None
            if not self.stopping.is_set():
                self.stopping.wait(delay)
                delay = min(delay * 2, 120)

    def _serve(self, request):
        try:
            result = self.providers.handle(request)
            body = dict(ok=True, result=result)
        except Refused as error:
            body = dict(ok=False, error=str(error))
        except Exception as error:  # noqa: BLE001
            log.warning("bridge request %s failed: %s", request.get("capability"), type(error).__name__)
            body = dict(ok=False, error=f"{type(error).__name__} on the desktop")
        try:
            with self._client() as client:
                client.post(f"companion/requests/{request['id']}", json=body).raise_for_status()
            self.served += 1
        except httpx.HTTPError as error:
            log.warning("bridge answer not delivered: %s", type(error).__name__)
