# Writing a plugin

Throughline plugins are independent packages that talk to the core exclusively over HTTP. You can write one in Python using [`throughline-sdk`](../sdk/README.md) or in any language against the [HTTP contract](plugin-http-contract.md). See `plugins/template` (Python) and `plugins/template-node` (Node.js) for working reference implementations.

## 1. Scaffold and implement

1. **Create the package.** Add `plugins/your-source/pyproject.toml`, `src/your_source/__init__.py`, `tests/test_source.py`, and `README.md`. Copy `plugins/template/pyproject.toml` using unique distribution, import, and console script names and a dependency on `throughline-sdk`. Root workspace members match `plugins/*`, so no shared config edits are needed.
2. **Declare a `Manifest`.** Provide clear channel descriptions, typed fields, `is_reference`/`is_outside` flags, optional `record_kind` and `embed` defaults, settings/secrets environment variable names, `run_mode`, `default_interval_s`, and descriptive `link_types`. Subclass `Plugin` and implement `records(cursor, mode)`.
3. **Yield records and checkpoints.** Yield normalized message dicts, optional `Record(kind=...)` / `provenance(...)` / `coverage(...)` objects, and `Checkpoint(cursor={...})` objects. A checkpoint must cover every record yielded before it. Stable `external_id` values make replay safe. Identity endpoints require an owner or agent token; look up existing natural keys before creating identity rows.
4. **Support backfill and sync in one generator.** Production source clients should accept a fixture transport or fixture path. Tests must use recorded responses only and never contact external networks.
5. **Expose the CLI entry point.** Export `main(): run(YourPlugin)`. Store obviously invented recordings under `tests/recorded/`. In pytest, call `check_plugin("your_source:YourPlugin", {"YOUR_INPUT": str(recording)}, secrets=[...])` from `throughline_sdk.contract` (pass a command list for non-Python plugins).
6. **Run the workspace checks.** Run `uv sync --all-packages`, then `THROUGHLINE_DEV=1 uv run pytest -q` and `uv run ruff check .` at the repository root. Pytest runs in `importlib` mode so test filenames can repeat across plugins.

7. **Describe it for onboarding.** Add an entry to your repository's `throughline-plugins.toml` (or a `throughline-plugin.toml` for a one-plugin repository) so the setup wizard can show it, ask for exactly the right credentials and back fill it. Override `Plugin.check()` to verify credentials with a clear message, and call `self.progress(done=, total=, unit=)` on long imports. See [the plugin onboarding contract](plugin-onboarding-contract.md); check it with `throughline lib plugins validate PATH`.

## 2. Fixtures and the contract kit

`plugins/template/tests/recorded/events.jsonl` is a minimal example with out-of-order historical messages. A contract-kit fixture must:

- cover every channel declared in the manifest,
- contain at most 10,000 messages,
- include several checkpointed batches so the kit can inject three distinct crash points, and
- contain **no real personal data** (neither in fixtures, recordings, nor logs).

The kit verifies normalized wire messages and raw payloads, typed channel views and field types, versioning and reparse, fresh-store sync, dry-run counts and date bounds, reversed delivery, duplicate replay, stale health, auxiliary state, and secret redaction.

Run it directly from the CLI:

```sh
uv run throughline-contract --entry your_source:YourPlugin
# or for another language:
uv run throughline-contract -- node plugins/template-node/plugin.mjs
```

Export the recorded-input environment variable first. The kit launches real core subprocesses against disposable `tl_test_*` databases (core and `psycopg` are test-time dependencies only, not runtime SDK dependencies). Both bundled templates run through it in CI.

## 3. Plugin token boundaries

In production, run every connector with its own scoped plugin token:

```sh
throughline token create your-source --kind plugin --plugin your_source --json
```

(The contract kit runs with an owner token so it can inspect the database and exercise all endpoints.)

- **Links:** a plugin token may only write `source`-method links, and each end must be a message in one of its own channels or an entity (`{kind:"entity", id}` or a `{kind, external_ref, name?}` placeholder, on either or both ends). Batch links may refer to messages delivered in the same batch. Links to people, to another plugin's messages, or with another method return `403` and roll the whole batch back. Manifest `link_types` are descriptive documentation, not an allow-list.
- **Location tracks:** writing track points requires `--location-source` on token creation (or `throughline token location-source <id>` for an existing token).
- **Identity writes:** identity and registry endpoints require an owner or agent token; plugin tokens cannot modify identity. Exact repeated active links and track points are replay-safe; identity creates are not automatically idempotent.

## 4. Provenance for pages and derived channels

A page or derived channel (`record_kind` of `page_revision` or `derived`) should record which source records each item came from:

```python
from throughline_sdk import provenance

yield provenance(
    derived={"channel": "my_pages.notes", "external_id": "page-42"},
    sources=[{"channel": "chat.sms", "external_id": "msg-101"}],
    relation="derived_from",
)
```

The runner flushes preceding messages and posts to `POST /v1/provenance` (skipped on dry runs). The derived record must belong to one of the plugin's own page or derived channels; sources may live in any channel. Split lists longer than 1,000 sources across multiple calls. Unresolved source keys are counted rather than rejected (`report["provenance"] = {records, written, unresolved}`), and re-sending existing citations is a no-op, so plugins can safely re-emit citations whose sources arrive later. A restricted asker sees a sourced record only when every source is visible; see [docs/v11/lineage.md](v11/lineage.md).

## 5. Outages and catch-up

Throughline expects every source to go offline occasionally — for hours or days — and to catch back up on its own when it returns. See [source health](v11/sources.md). For a plugin that means:

- **Resume from the cursor, never from now.** Checkpoint the newest item you have actually yielded, not the wall-clock run time, so the first run after an outage picks up exactly where the last good run stopped.
- **Fail when the source is unreachable.** If every fetch in a run fails, raise: the runner sends a failure heartbeat so the source transitions to `failing` and then `down`. Swallowing errors makes the run look healthy and empty, leaving only the slower arrival-cadence check to notice. If only some scopes failed, continue and yield `coverage(..., status="partial", reasons=["source_error"])` for them.
- **Widen fixed lookbacks.** A plugin that re-reads "the last N days" should start from `self.catch_up_since(timedelta(days=N))` — the earlier of `now - N` and the plugin's last successful run (fetched from the core before `records()`) — so an outage longer than N days still catches up.
- **Report what could not be recovered.** A source that only serves its newest items (a recently-played list, an RSS feed) cannot backfill a long outage. When a full page's oldest item is newer than your stored high-water mark, yield `coverage(channel, status="partial", reasons=["source_cap"], gaps=[{"from": high_water, "to": oldest_item, "reason": "source_cap"}])`. The outage closes as `partial` with that reason, and recall over that window discloses the gap.
- **Run continuously or on a timer.** `sync` without `--once` never exits on a failed run: it retries after 1, 2, 4 … minutes (capped at the interval). Under a timer (`sync --once`), each failed run exits 1 and the next tick retries from the cursor. Set `THROUGHLINE_INTERVAL_S` to match your timer cadence so down detection stays accurate.

## Asking the desktop

A connector that runs on the server but needs something only the owner's
desktop has (signed-in browser cookies, a file in a shared folder, a
localhost app) asks the [Throughline Companion](companion.md) instead of
keeping a copy:

```python
from throughline_sdk import DesktopUnavailable

def records(self, cursor, mode):
    try:
        cookies = self.desktop("cookies", {"domain": "example.com"})
    except DesktopUnavailable as error:
        # error.reason: no_device, not_offered, offline, timeout, failed
        raise RuntimeError(f"desktop cookies unavailable: {error.reason}") from None
    ...
```

The owner must switch the capability on in the Companion and allow your
plugin name. Without code changes, the Docker runner can also inject a
desktop value into a setting (`{"desktop": "cookies", ..., "as": "header"}`;
see [companion.md](companion.md#the-bridge)).

## 6. Real connectors in this workspace

Real source connectors under `plugins/` (excluding `template` and `template-node`) are maintained in separate private repositories and attached as git submodules. Clone with `--recurse-submodules` (or run `git submodule update --init`) when developing against them, and commit connector changes inside each submodule before updating the pointer in the parent repo.
