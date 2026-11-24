import hashlib
import json
import secrets
import time

import httpx

# Links per POST /v1/links/batch (the core's limit).
LINK_BATCH = 1000


class Client:
    def __init__(self, url, token, plugin, *, retries=4, transport=None):
        self.plugin = plugin
        self.retries = retries
        self.http = httpx.Client(
            base_url=url.rstrip("/") + "/v1/",
            headers={"Authorization": "Bearer " + token},
            timeout=60,
            transport=transport,
        )

    def close(self):
        self.http.close()

    def request(self, method, path, data=None, *, retry=False, **kwargs):
        for attempt in range(self.retries + 1):
            try:
                response = self.http.request(method, path, json=data, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                return response.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                retryable = (
                    not isinstance(error, httpx.HTTPStatusError)
                    or error.response.status_code == 429
                    or error.response.status_code >= 500
                )
                if not retry or not retryable or attempt == self.retries:
                    raise
                time.sleep(min(0.1 * 2**attempt, 5))

    def register(self, manifest):
        self.request(
            "POST", f"plugins/{self.plugin}/manifest", manifest.model_dump(), retry=True
        )
        for channel in manifest.channels:
            self.request(
                "POST",
                "channels",
                dict(plugin=self.plugin, **channel.model_dump()),
                retry=True,
            )

    def ingest(self, messages, *, links=None, dry_run=False):
        results = []
        for start in range(0, max(1, len(messages)), 5000):
            result = self.request(
                "POST",
                "ingest/messages",
                dict(
                    plugin=self.plugin,
                    messages=messages[start : start + 5000],
                    links=(links or []) if start + 5000 >= len(messages) else [],
                    dry_run=dry_run,
                ),
                retry=True,
            )
            if result["rejected"]:
                raise ValueError("batch contains rejected messages; cursor retained")
            results.append(result)
        return results

    def cursor(self, key="default"):
        try:
            return self.request(
                "GET", f"plugins/{self.plugin}/cursor/{key}", retry=True
            )["cursor"]
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                return {}
            raise

    def checkpoint(self, cursor, key="default"):
        return self.request(
            "PUT", f"plugins/{self.plugin}/cursor/{key}", cursor, retry=True
        )

    def heartbeat(self, **data):
        return self.request("POST", f"plugins/{self.plugin}/heartbeat", data)

    def health(self):
        """This plugin's source health as the core sees it: state, last
        success, the open outage. {} from a core without the route."""
        try:
            return self.request("GET", f"plugins/{self.plugin}/health", retry=True)
        except httpx.HTTPStatusError as error:
            if error.response.status_code in (404, 405):
                return {}
            raise

    def idempotency_key(self, route, data, delivery=""):
        """The Idempotency-Key of a record write: sha256 over the plugin name,
        the route, a delivery id and the canonical JSON of the record.

        The SDK draws a fresh delivery id for every logical delivery and keeps
        it across that delivery's retries, so a retry of a request the core
        already committed (a timeout, a 5xx after commit) is a no-op replay.
        A later delivery of the same record (the next run, a re-sent citation)
        gets a new key and is processed again: the core's own rules keep that
        idempotent (existing links and provenance rows are kept), and an end
        or a cited record imported since then now resolves (BRN-6)."""
        canonical = json.dumps(
            data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
        )
        digest = hashlib.sha256(
            "\n".join((self.plugin, route, delivery, canonical)).encode()
        ).hexdigest()
        return "tl-" + digest

    def _keyed(self, route, data):
        delivery = secrets.token_hex(8)
        return {"Idempotency-Key": self.idempotency_key(route, data, delivery)}

    def link(self, data):
        return self.request(
            "POST", "links", data, retry=True, headers=self._keyed("links", data)
        )

    def link_batch(self, links):
        """Deliver links through POST /v1/links/batch, at most 1,000 per
        request. Returns one result per link, in order: {index, status
        (created, existing, folded, candidate, refused, error), reason,
        link_id}. A vocabulary refusal or a missing end is a result, not an
        error; an authorization failure (403) raises for the whole batch.
        A core without the batch route gets one POST /v1/links per link."""
        results = []
        for start in range(0, len(links), LINK_BATCH):
            chunk = list(links[start : start + LINK_BATCH])
            body = {"links": chunk}
            try:
                answer = self.request(
                    "POST",
                    "links/batch",
                    body,
                    retry=True,
                    headers=self._keyed("links/batch", body),
                )
            except httpx.HTTPStatusError as error:
                if error.response.status_code not in (404, 405):
                    raise
                answer = {
                    "results": [self._single(i, link) for i, link in enumerate(chunk)]
                }
            for item in answer.get("results") or []:
                item = dict(item)
                item["index"] = start + int(item.get("index") or 0)
                results.append(item)
        return results

    def _single(self, index, link):
        """One link through POST /v1/links (cores before the batch route)."""
        try:
            row = self.link(link)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 400:
                raise
            try:
                body = error.response.json()
            except ValueError:
                body = {}
            return {
                "index": index,
                "status": "refused" if body.get("code") == "23514" else "error",
                "reason": body.get("detail"),
                "link_id": None,
            }
        outcome = (row.get("receipt") or {}).get("outcome") or "created"
        return {
            "index": index,
            "status": outcome,
            "reason": None,
            "link_id": row.get("id"),
        }

    def provenance(self, data):
        """Deliver one provenance record; the core answers {written,
        unresolved, receipt}."""
        return self.request(
            "POST",
            "provenance",
            data,
            retry=True,
            headers=self._keyed("provenance", data),
        )

    def track(self, points):
        return self.request("POST", "ingest/track", {"points": points}, retry=True)

    def identity(self, operation, data=None, method="POST"):
        return self.request(method, "identity/" + operation, data)

    def registry(self, resource, data=None, method="POST"):
        return self.request(method, "registry/" + resource, data)

    def coverage(self, reports):
        """Deliver coverage reports; the core answers {stored, degraded,
        refused, gaps_opened: [{index, id, from, to}], gaps_closed}."""
        reports = [r.data if hasattr(r, "data") else dict(r) for r in reports]
        body = {"reports": reports}
        return self.request(
            "POST",
            f"plugins/{self.plugin}/coverage",
            body,
            retry=True,
            headers=self._keyed("coverage", body),
        )

    def coverage_attempt(self, channels=None):
        """Note an import attempt for these channels (all when None)."""
        body = {} if channels is None else {"channels": list(channels)}
        return self.request("POST", f"plugins/{self.plugin}/coverage/attempt", body)

    def coverage_gaps(self, channel=None, scope_key=None):
        """Open gaps of this plugin's channels, with ids to close them by."""
        params = {}
        if channel is not None:
            params["channel"] = channel
        if scope_key is not None:
            params["scope_key"] = scope_key
        return self.request(
            "GET", f"plugins/{self.plugin}/coverage/gaps", params=params, retry=True
        )["gaps"]

    def desktop(self, capability, args=None, *, device=None, timeout_s=30):
        """Ask a Throughline Companion device for something only the desktop
        has (POST /v1/companion/call): browser cookies for a site, an
        allow-listed file, a localhost service. Returns the device's answer.

        Raises DesktopUnavailable with .reason no_device, not_offered (the
        owner has not allowed this plugin in the companion app), offline,
        timeout or failed."""
        body = {"capability": capability, "args": args or {}, "timeout_s": timeout_s}
        if device:
            body["device"] = device
        try:
            response = self.http.post(
                "companion/call", json=body, timeout=timeout_s + 15
            )
        except httpx.TransportError as error:
            raise DesktopUnavailable("unreachable", str(error)) from None
        if response.status_code == 200:
            return response.json()["result"]
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        if isinstance(detail, dict):
            raise DesktopUnavailable(detail.get("reason") or "failed", detail.get("message"))
        raise DesktopUnavailable(
            "not_supported" if response.status_code in (404, 405) and detail == "Not Found" else "failed",
            str(detail or response.status_code),
        )


class DesktopUnavailable(Exception):
    """A companion bridge call that got no answer; .reason says why."""

    def __init__(self, reason, message=None):
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason
