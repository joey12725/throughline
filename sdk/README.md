# throughline-sdk

Independent Python 3.12+ SDK for building Throughline source plugins (`import throughline_sdk`). Runtime dependencies are `httpx` and `pydantic` only. The Throughline core never imports this package; all communication goes over HTTP.

## Quick example

```python
from datetime import timedelta
from throughline_sdk import Channel, Checkpoint, Field, Manifest, Plugin, run


class ExamplePlugin(Plugin):
    manifest = Manifest(
        name="example",
        version="0.1.0",
        channels=[
            Channel(
                name="example.chat",
                description="Example chat messages",
                fields=[Field(name="rating", type="integer", description="Optional score")],
                role="chat",  # optional: what the channel holds (see below)
            )
        ],
        run_mode="poll",
        default_interval_s=3600,
    )

    def records(self, cursor, mode):
        since = self.catch_up_since(timedelta(days=7))
        for batch in fetch_batches(cursor, since=since):
            for item in batch.items:
                yield {
                    "channel": "example.chat",
                    "external_id": item["id"],
                    "thread": {"external_id": item["thread_id"]},
                    "happened_at": item["timestamp"],
                    "content": item["text"],
                    "fields": {"rating": item.get("rating")},
                    "payload": item,
                }
            yield Checkpoint(cursor={"after": batch.next_cursor})


def main():
    run(ExamplePlugin)
```

A channel may declare an optional `role` so core features find it by kind rather than by name: `chat`, `calendar`, `telemetry`, `transcript`, `voice_summary`, `location`, `email` (or another lowercase word). `orient_priority` orders it in `POST /v1/orient` (higher first) and `context_window_min` is the smallest thread window `POST /v1/find` reads around a hit when the caller did not choose one (useful for transcripts). All three are optional; a channel that declares nothing is treated neutrally.

Exposing `main()` gives your plugin the standard CLI: `manifest`, `backfill [--dry-run]`, and `sync [--once]`.

## What the runner handles for you

- **Checkpointing & crash safety.** Flushes all preceding message, link, provenance, track and coverage records before writing a `Checkpoint` cursor. Yield regular checkpoints to bound replay after a crash.
- **Write contract.** Automatically attaches deterministic `Idempotency-Key` headers to link, link-batch, provenance and coverage requests, batches consecutive link records through `POST /v1/links/batch`, and passes message `aliases` through unchanged.
- **Outages & catch-up.** Reads `GET /v1/plugins/{name}/health` into `self.health` before `records()`, provides `self.catch_up_since(delta)` to widen fixed lookbacks after downtime, sends success/failure heartbeats automatically, and retries continuously with exponential backoff in `sync` mode.
- **Secret redaction.** Reads settings and secrets from the manifest's declared environment variables, redacts secret values from its log handler, and refuses to transmit records containing declared secrets. Always use `logging` rather than printing credentials or attaching unfiltered handlers.
- **Auxiliary records.** Yield `Record(kind="link"|"track"|"identity", ...)` (identity records include an HTTP `path`, e.g. `registry/identifiers`), `provenance(derived, sources, relation=...)`, and `coverage(...)` alongside messages.

## HTTP client (`Client`)

`throughline_sdk.Client` exposes channel registration, message ingest, cursors, heartbeats, health, links, track points, provenance, coverage, identity, registry, and generic HTTP requests. It retries idempotent writes on transport errors, 429 and 5xx with bounded exponential backoff (non-idempotent identity creates and heartbeat deltas are not retried automatically). Close the client when finished or use it as a context manager.

## Contract test harness

`throughline_sdk.contract.check_plugin` (pytest helper) and the `throughline-contract` CLI verify a plugin against real disposable Throughline core subprocesses and scratch `tl_test_*` PostgreSQL databases:

```sh
uv run throughline-contract --entry your_source:YourPlugin
# or any language:
uv run throughline-contract -- node plugins/template-node/plugin.mjs
```

Recorded fixtures must cover every declared channel, stay under 10,000 messages, and never contain real personal data.

## Further reading

- [Writing a plugin](../docs/writing-a-plugin.md) — step-by-step setup, provenance, outages and catch-up
- [Plugin HTTP contract](../docs/plugin-http-contract.md) — raw HTTP wire shapes for any language
