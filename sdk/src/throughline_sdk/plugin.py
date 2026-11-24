import argparse
import json
import logging
import os
import time
from collections import Counter
from datetime import datetime

from . import infisical
from .client import LINK_BATCH, Client
from .models import Checkpoint, Record

TRACK_BATCH = 10000


def contains_secret(value, secrets):
    if isinstance(value, str):
        return any(secret and secret in value for secret in secrets)
    if isinstance(value, dict):
        return any(
            contains_secret(k, secrets) or contains_secret(v, secrets)
            for k, v in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_secret(v, secrets) for v in value)
    return False


class Redactor(logging.Filter):
    def __init__(self, values):
        super().__init__()
        self.values = sorted(filter(None, values), key=len, reverse=True)

    def add(self, values):
        combined = set(self.values)
        combined.update(filter(None, values))
        self.values = sorted(combined, key=len, reverse=True)

    def clean(self, text):
        for value in self.values:
            text = text.replace(value, "[REDACTED]")
        return text

    def filter(self, record):
        record.msg = self.clean(record.getMessage())
        record.args = ()
        if record.exc_info:
            import traceback

            record.msg += "\n" + self.clean(
                "".join(traceback.format_exception(*record.exc_info))
            )
            record.exc_info = None
        return True


def _progress_stream():
    """Where structured progress lines go: THROUGHLINE_PROGRESS=stderr (or 1)
    writes them to stderr, any other value is a file path appended to. Unset,
    nothing is written. Each line is `TL-PROGRESS {json}`; the onboarding
    runner and the web UI read them to draw a progress bar."""
    target = os.getenv("THROUGHLINE_PROGRESS")
    if not target:
        return None
    if target in ("1", "stderr"):
        import sys

        return sys.stderr
    return open(target, "a", buffering=1)


class Progress:
    """Throttled progress reporter (at most one line a second, plus the
    final one). Fields: delivered (messages), tracks (location points),
    oldest/newest (ISO time of the delivered span), done/total/unit/note
    when the plugin knows them (self.progress(...))."""

    def __init__(self):
        self.stream = _progress_stream()
        self.state = dict(delivered=0, tracks=0, oldest=None, newest=None)
        self.last = 0.0

    def update(self, force=False, **fields):
        for key, value in fields.items():
            if value is None:
                continue
            if key == "oldest":
                old = self.state.get("oldest")
                value = value if old is None or value < old else old
            elif key == "newest":
                old = self.state.get("newest")
                value = value if old is None or value > old else old
            self.state[key] = value
        if self.stream is None:
            return
        now = time.monotonic()
        if force or now - self.last >= 1.0:
            self.last = now
            try:
                self.stream.write("TL-PROGRESS " + json.dumps(self.state, default=str) + "\n")
                self.stream.flush()
            except (OSError, ValueError):
                self.stream = None


class Plugin:
    manifest = None

    def __init__(self):
        self.settings = {}
        self.secrets = {}
        infisical_client = None
        for target, declarations in (
            (self.settings, self.manifest.settings),
            (self.secrets, self.manifest.secrets),
        ):
            for name, declaration in declarations.items():
                value = os.getenv(declaration.env, declaration.default)
                if value is not None and infisical.is_ref(value):
                    if infisical_client is None:
                        infisical_client = infisical.InfisicalClient(
                            infisical.InfisicalConfig.from_settings()
                        )
                    value = infisical_client.get_secret(value)
                if declaration.required and value is None:
                    raise ValueError("missing environment variable " + declaration.env)
                target[name] = value

    def records(self, cursor, mode):
        raise NotImplementedError

    def check(self):
        """Verify the configuration without importing anything: reach the
        source, authenticate, read one item. Return a short human sentence
        (or a dict with message/detail) on success and raise with a clear,
        actionable message on failure. The default only proves that every
        required setting is present (the constructor already enforced it)."""
        return "Every required setting is present."

    def progress(self, done=None, total=None, unit=None, note=None):
        """Report how far a long run is (e.g. done=120, total=900,
        unit="recordings"). Optional: without it the runner still reports
        delivered counts and the time span covered so far."""
        reporter = getattr(self, "_progress", None)
        if reporter is not None:
            reporter.update(done=done, total=total, unit=unit, note=note)

    def desktop(self, capability, args=None, **options):
        """Ask the owner's Throughline Companion for something only the
        desktop has (see Client.desktop). Usable inside records()."""
        client = getattr(self, "_throughline_client", None)
        if client is None:
            from .client import DesktopUnavailable

            raise DesktopUnavailable("no_client", "desktop() is only available during a run")
        return client.desktop(capability, args, **options)

    def catch_up_since(self, lookback):
        """Start of a fixed lookback window (a timedelta), widened to cover
        an outage: the earliest of now - lookback, this plugin's last
        successful run and the start of its open outage (self.health, read
        before records()). A plugin that
        re-reads "the last N days" uses this so a source that was down for
        longer than N days still catches up completely."""
        from datetime import timezone

        start = datetime.now(timezone.utc) - lookback
        health = getattr(self, "health", None) or {}
        # A stall keeps runs "succeeding": its outage says since when the
        # data was last good.
        for value in (
            health.get("last_success_at"),
            (health.get("outage") or {}).get("last_good_at"),
        ):
            if not value:
                continue
            try:
                when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            start = min(start, when)
        return start

    def execute(self, client, mode, dry_run=False):
        """Run one import. Coverage: an attempt is posted for the manifest
        channels before records(); coverage records are posted after the
        messages yielded before them; channels that delivered messages
        without a coverage record get a best_effort report with
        rows_delivered and high_water; a failure posts partial with reason
        run_failed and a redacted detail, and never masks the original
        error. A dry run sends no coverage."""
        client.register(self.manifest)
        self._throughline_client = client
        self._progress = Progress()
        channels = [c.name for c in self.manifest.channels]
        self.health = {}
        if not dry_run:
            _best_effort("coverage attempt", client.coverage_attempt, channels)
            self.health = _best_effort("health", client.health) or {}
        try:
            return self._execute(client, mode, dry_run)
        except BaseException as error:
            if not dry_run:
                redactor = Redactor(
                    list(self.secrets.values()) + [os.getenv("THROUGHLINE_TOKEN", "")]
                )
                detail = {
                    "error": type(error).__name__,
                    "message": redactor.clean(str(error))[:500],
                }
                _best_effort(
                    "coverage failure report",
                    client.coverage,
                    [
                        {
                            "channel": name,
                            "status": "partial",
                            "reasons": ["run_failed"],
                            "detail": detail,
                        }
                        for name in channels
                    ],
                )
            raise

    def _execute(self, client, mode, dry_run):
        counts = Counter()
        dates = []
        pending = []
        total = 0
        cited = Counter()
        covered = Counter()
        gap_ids = []
        delivered = {}
        reported = set()
        plugin_level = False
        links = []
        linked = Counter()
        tracks = []

        def flush_tracks():
            # Consecutive track points go out together (the route takes up to
            # TRACK_BATCH): each request re-runs late location attachment.
            if tracks:
                client.track(list(tracks))
                stamps = [t.get("at") for t in tracks if t.get("at")]
                self._progress.update(
                    tracks=self._progress.state["tracks"] + len(tracks),
                    oldest=min(stamps) if stamps else None,
                    newest=max(stamps) if stamps else None,
                )
                tracks.clear()

        def flush_links():
            # Consecutive link records go out together through the batch
            # route. Vocabulary refusals, candidates and missing ends are
            # counted in the report; an authorization failure raises.
            if links:
                for result in client.link_batch(links):
                    linked[result.get("status") or "error"] += 1
                linked["records"] += len(links)
                links.clear()

        def flush():
            nonlocal total
            if pending:
                client.ingest(pending, dry_run=dry_run)
                for message in pending:
                    seen = delivered.setdefault(message["channel"], [0, None])
                    seen[0] += 1
                    when = message.get("happened_at")
                    if when and (seen[1] is None or when > seen[1]):
                        seen[1] = when
                total += len(pending)
                stamps = [m.get("happened_at") for m in pending if m.get("happened_at")]
                self._progress.update(
                    delivered=total,
                    oldest=min(stamps) if stamps else None,
                    newest=max(stamps) if stamps else None,
                )
                pending.clear()

        def coverage_answer(answer):
            answer = answer if isinstance(answer, dict) else {}
            for key in ("stored", "refused"):
                covered[key] += int(answer.get(key) or 0)
            covered["degraded"] += len(answer.get("degraded") or [])
            gap_ids.extend(
                g["id"]
                for g in answer.get("gaps_opened") or []
                if isinstance(g, dict) and g.get("id")
            )
            covered["gaps_closed"] += len(answer.get("gaps_closed") or [])

        for item in self.records({} if dry_run else client.cursor(), mode):
            value = (
                item.model_dump() if isinstance(item, (Record, Checkpoint)) else item
            )
            if contains_secret(value, self.secrets.values()):
                raise ValueError("declared secret found in generated record")
            is_link = isinstance(item, Record) and item.kind == "link"
            is_track = isinstance(item, Record) and item.kind == "track"
            if not is_link:
                flush_links()
            if not is_track:
                flush_tracks()
            if isinstance(item, Checkpoint):
                flush()
                if not dry_run:
                    client.checkpoint(item.cursor, item.key)
            else:
                item = item if isinstance(item, Record) else Record(data=item)
                if item.kind == "message":
                    counts[item.data["channel"]] += 1
                    dates.append(datetime.fromisoformat(item.data["happened_at"]))
                    pending.append(item.data)
                    if len(pending) == 5000:
                        flush()
                elif not dry_run:
                    flush()
                    if item.kind == "track":
                        tracks.append(item.data)
                        if len(tracks) == TRACK_BATCH:
                            flush_tracks()
                    elif item.kind == "link":
                        links.append(item.data)
                        if len(links) == LINK_BATCH:
                            flush_links()
                    elif item.kind == "provenance":
                        # Delivered after the messages it refers to, like links.
                        answer = client.provenance(item.data) or {}
                        cited["records"] += 1
                        for key in ("written", "unresolved"):
                            cited[key] += int(answer.get(key) or 0)
                    elif item.kind == "coverage":
                        # After the messages yielded before it, like provenance.
                        covered["records"] += 1
                        coverage_answer(client.coverage([item.data]))
                        if item.data.get("channel") is None:
                            plugin_level = True
                        elif not item.data.get("scope_key"):
                            # A report for one scope (an account, a feed)
                            # keeps the channel-wide automatic report.
                            reported.add(item.data["channel"])
                    else:
                        if not item.path:
                            raise ValueError("identity record needs path")
                        client.request("POST", item.path, item.data)
        flush_links()
        flush_tracks()
        flush()
        self._progress.update(force=True, finished=True)
        if not dry_run:
            automatic = [
                {
                    "channel": name,
                    "status": "best_effort",
                    "rows_delivered": rows,
                    "high_water": high_water,
                }
                for name, (rows, high_water) in sorted(delivered.items())
                if name not in reported and not plugin_level
            ]
            if automatic:
                answer = _best_effort("coverage report", client.coverage, automatic)
                if answer is not None:
                    covered["automatic"] += len(automatic)
                    coverage_answer(answer)
            client.heartbeat(interval_s=interval_of(self), messages_ingested=total)
        report = dict(
            counts=dict(counts),
            start=min(dates).isoformat() if dates else None,
            end=max(dates).isoformat() if dates else None,
            estimated_model_cost=0,
        )
        if linked:
            report["links"] = {
                key: linked[key]
                for key in (
                    "records",
                    "created",
                    "existing",
                    "folded",
                    "candidate",
                    "refused",
                    "error",
                )
            }
            if linked["refused"] or linked["error"]:
                logging.getLogger("throughline_sdk").warning(
                    "%d links refused by the vocabulary, %d not stored",
                    linked["refused"],
                    linked["error"],
                )
        if cited:
            report["provenance"] = {
                key: cited[key] for key in ("records", "written", "unresolved")
            }
        if covered or gap_ids:
            report["coverage"] = dict(
                {
                    key: covered[key]
                    for key in (
                        "records",
                        "automatic",
                        "stored",
                        "degraded",
                        "refused",
                        "gaps_closed",
                    )
                },
                gaps_opened=gap_ids,
            )
        return report


def _best_effort(what, function, *args):
    """Call a coverage endpoint; a failure is logged and never raised."""
    try:
        return function(*args)
    except Exception as error:
        logging.getLogger("throughline_sdk").warning(
            "%s not delivered: %s", what, type(error).__name__
        )
        return None


def run(plugin_type, argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["manifest", "check", "backfill", "sync"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "manifest":
        print(plugin_type.manifest.model_dump_json())
        return
    redactor = Redactor(
        [os.getenv(d.env, d.default) for d in plugin_type.manifest.secrets.values()]
        + [os.getenv("THROUGHLINE_TOKEN", "")]
    )
    handler = logging.StreamHandler()
    handler.addFilter(redactor)
    root = logging.getLogger()
    root.handlers = [handler]
    if args.command == "check":
        _check(plugin_type, redactor)
        return
    client = None
    try:
        plugin = plugin_type()
        redactor.add(plugin.secrets.values())
        token = infisical.resolve_value(os.environ["THROUGHLINE_TOKEN"])
        redactor.add([token])
        client = Client(
            os.environ["THROUGHLINE_URL"],
            token,
            plugin.manifest.name,
        )
        if args.command == "backfill" or args.once or args.dry_run:
            print(json.dumps(plugin.execute(client, args.command, args.dry_run)))
            return
        _loop(plugin, client, redactor)
    except Exception as error:
        safe = redactor.clean(str(error))
        logging.error(safe)
        if client and not args.dry_run:
            try:
                client.heartbeat(error=safe)
            except Exception as heartbeat_error:
                logging.error(redactor.clean(str(heartbeat_error)))
        raise SystemExit(1) from None
    finally:
        if client:
            client.close()


def _check(plugin_type, redactor):
    """`check`: one JSON line {ok, message, detail?}; exit 1 when not ok.
    Messages are redacted so a pasted key never echoes back."""
    try:
        plugin = plugin_type()
        answer = plugin.check()
    except Exception as error:
        message = redactor.clean(str(error)) or type(error).__name__
        print(json.dumps({"ok": False, "message": message, "error": type(error).__name__}))
        raise SystemExit(1) from None
    if isinstance(answer, dict):
        body = {"ok": True, **{k: v for k, v in answer.items() if k != "ok"}}
    else:
        body = {"ok": True, "message": str(answer or "Connected.")}
    print(redactor.clean(json.dumps(body, default=str)))


def interval_of(plugin):
    """Seconds between runs: THROUGHLINE_INTERVAL_S when set (a timer that
    runs `sync --once` on its own schedule should set it to that schedule),
    else the manifest's default. The core judges a source down after two
    intervals without success, so this must match how often it really runs."""
    try:
        value = int(os.getenv("THROUGHLINE_INTERVAL_S") or 0)
    except ValueError:
        value = 0
    return value if value > 0 else plugin.manifest.default_interval_s


def retry_delays(interval_s, first=60):
    """Seconds to wait after each failed run in a row: 1, 2, 4, ... minutes,
    never longer than the plugin's interval."""
    interval_s = max(1, int(interval_s))
    delay = min(first, interval_s)
    while True:
        yield delay
        delay = min(delay * 2, interval_s)


def _loop(plugin, client, redactor, *, sleep=time.sleep, runs=None):
    """`sync` without --once: run forever. A failed run (the source or
    Throughline unreachable, an error in records()) is logged and reported
    as a failure heartbeat, and the next run is tried after a growing delay
    (1 minute doubling up to the interval). The cursor only moves at
    checkpoints, so the first run that succeeds after an outage resumes
    where the last good one stopped and catches up. runs bounds the loop
    (tests)."""
    interval = interval_of(plugin)
    delays = None
    done = 0
    while runs is None or done < runs:
        done += 1
        try:
            print(json.dumps(plugin.execute(client, "sync")), flush=True)
        except Exception as error:
            safe = redactor.clean(str(error))
            delays = delays or retry_delays(interval)
            wait = next(delays)
            logging.error("run failed, retrying in %ss: %s", wait, safe)
            try:
                client.heartbeat(error=safe or type(error).__name__)
            except Exception as heartbeat_error:
                logging.error(
                    "failure heartbeat not delivered: %s",
                    redactor.clean(str(heartbeat_error)),
                )
        else:
            delays = None
            wait = interval
        if runs is None or done < runs:
            sleep(wait)
