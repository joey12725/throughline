# Onboarding

A new Throughline install is set up in the browser at **`/ui/welcome`**, or
by any AI agent following the setup prompt. Both use the same owner-only API
(`/v1/setup/*`), so everything the wizard does an agent can do too.

```
throughline serve
# ─────────────────────────────────────────────────────────
#   Welcome to Throughline. Finish setup in your browser:
#     http://127.0.0.1:8000/ui/welcome?setup=7F3A-91C2-0B5E
# ─────────────────────────────────────────────────────────
```

## The steps

| Step | What happens |
|---|---|
| **Welcome** | Fresh start, restoring/continuing, or moving from another system. If the database already holds data, existing sources are shown as already connected and skipped. |
| **Account** | Create the owner account. Needs the one-time setup code the server printed (also in `~/.throughline/config/setup-code`, or `throughline lib setup code`). |
| **Intelligence** | Bring your own keys: OpenRouter, Voyage, OpenAI, Anthropic, Gemini, a CLI proxy (any OpenAI-compatible URL) or local models (Ollama, LM Studio). Every key is tested live before it's saved. Pick providers for embeddings (meaning search), the assistant, and the Librarian/Lenses. |
| **Photos** | Connect an existing Immich instance (URL + API key). |
| **Sources** | Pick what you actually use from the catalog: popular picks first, search and categories for the rest. Paste a GitHub link to add a third-party plugin repository. |
| **Connect** | One card per source: plain-language steps, only the fields it needs, uploads for export files (Google Timeline's `Timeline.json`), install, save and a live test. |
| **Companion** | For sources that read your computer (coding-agent logs, browser history, iMessage…): download the desktop app and paste a pairing code. |
| **Import** | Backfill history now, with a live progress bar per source (records, how far back, rate, ETA) and readable errors with a Retry. |
| **People** | Confirm which accounts are you; the resolver links people across sources (certain matches automatically, the rest as short questions). |
| **Lenses** | Pick starter lenses (plans & commitments, recommendations, important dates) built for the sources you connected, choose a **pace** (live, balanced, thrifty: freshness versus model calls), see the cost estimate, and run one to see its first rows. |
| **Pages** | Turn on daily pages, person profiles and event pages (each a pages lens, docs/v11/pages.md) at the same pace, optionally mirrored as Markdown files through a pages.toml sink, and write the first ones. |
| **Queries** | Starter questions chosen from your data (search, who you talk to most, your week, a person's brief, where you've been, your lenses), each runnable in place, with the request shown so you learn to write your own. |
| **Agent** | Connect Claude Code, Claude Desktop, Cursor, Codex, ChatGPT or any MCP client to `/v1/mcp`, with a token minted for it. |
| **Tour** | What each screen is for, and what to ask first. |

Every step can be skipped and revisited (`/ui/welcome/<step>`, or the command
palette's "Run setup again").

## Pace: freshness versus cost

Lenses and pages call the model chosen for the Librarian in the Intelligence
step (through `THROUGHLINE_TABLE_LLM_*`, which that choice sets). One **pace**
sets how often they run and how much each run may spend:

| Pace | Lenses | Daily pages | First run reads | Per-run budget |
|---|---|---|---|---|
| Live | every 30 min | every 30 min (a day at most hourly) | 30 days | $0.50 |
| Balanced | every 3 h | every 3 h (a day at most every 4 h) | 14 days | $0.25 |
| Thrifty | nightly | nightly | 7 days | $0.10 |

A CLI proxy or a local model makes every pace free per call. Lenses and pages
written by onboarding are ordinary files and definitions: edit
`~/.throughline/pages/*.toml` or the lens definitions any time.

## Restored and migrated installs

Detection (`GET /v1/setup/detect`) looks at what the database already holds:
any plugin with records in its declared channels (or track points in its
track sources) is **already connected**, so onboarding checks it off and only
offers what's left.

You can restore all or selected parts of a backup bundle (`database`, `config`,
`lenses`, `pages`) without losing onboarding — existing config keys are merged
by default, and onboarding stays open in `restore` mode so you can add more
sources afterwards:

```bash
throughline lib backup restore [BUNDLE] --only config,lenses,pages --continue-onboarding
```

Or use the **Partial restore from backup** panel inside the Welcome step
(`GET`/`POST /v1/setup/restore`) to restore selected scopes from a bundle on
the server while keeping existing sources intact. Force the "welcome back" path
with any of:

- `?resume=1` on the welcome URL,
- `THROUGHLINE_SETUP_MODE=restore` in the server environment,
- `throughline lib setup restored` (writes the restore marker and keeps
  `tl.setup_state` open in `restore` mode).

## Where settings live

Everything entered in the browser is written to two owner-only (0600) files in
`THROUGHLINE_SETUP_DIR` (default `~/.throughline/config`): `settings.json` and
`secrets.json`. On start the server applies them as environment variables, so
the rest of Throughline is unchanged: it still reads `VOYAGE_API_KEY`,
`THROUGHLINE_EMBEDDING_PROVIDER` and friends. A variable that is set in the
server's real environment always wins; the wizard shows it as locked.

- Secrets are never returned by the API (only `••••last4`), and are redacted
  from job logs and error messages.
- Saving a provider adds its host to `THROUGHLINE_EGRESS_ALLOW` (unless you
  manage that variable yourself).
- Plugin processes get only harmless server variables (PATH, HOME, locale,
  proxies), their own settings, and their own scoped plugin token, never the
  database URL or the server's model keys.

## Owner connection

Tokens, accounts and companion devices are created with the owner database
connection. A local install has it; the Docker `throughline` service normally
doesn't. When it's missing, each affected step shows the exact command to run
on the server instead (`docker compose run --rm migrate throughline …`).

## Plugins, installs and the scheduler

- The **starter** plugins are the `plugins/` directory of a source checkout
  (`THROUGHLINE_STARTER_PLUGINS`), else the public starter repository, cloned
  on first use (`THROUGHLINE_STARTER_REPO`).
- **Third-party** repositories are added by GitHub link (shallow clone into
  `THROUGHLINE_SETUP_DATA`, default `~/.throughline/setup`) and must follow
  the [plugin onboarding contract](plugin-onboarding-contract.md). Installing
  runs code from that repository on the server: the wizard says so.
- Python plugins are installed into one plugin environment
  (`<setup data>/env`) with `uv` (or pip), with the local `throughline-sdk`.
- After a successful backfill a source is scheduled: the server runs
  `sync --once` at the plugin's interval (`THROUGHLINE_SETUP_SCHEDULER=0`
  turns this off when you run plugins with systemd, Docker or the companion
  instead).

## The people resolver

`POST /v1/setup/people/resolve` reads every identifier the imports created
(with the display names sources gave them, their message counts and shared
conversations) and:

- links **certain matches** without asking (confidence ≥ 0.9): the same
  e-mail or phone in two systems; the same full name in two systems that also
  share a conversation; a lone identifier whose full name matches exactly one
  existing person (e.g. from GBrain);
- turns **unclear matches** (0.55–0.9) into questions with their evidence:
  the same full name without other evidence, near-identical names, a username
  spelled like an e-mail address elsewhere.

Its own decisions are asserted as `agent` and can be split later like any
merge; your answers are owner assertions, and "different people" records a
not-same pair so the question never comes back.

## Setting up with an agent

`GET /v1/setup/prompt` (or `throughline lib setup prompt --url …`) returns a
prompt to paste into any capable agent along with an owner token. It walks the
agent through the same steps, calling the same API, and tells it to ask you
only for things only you can give.

## API

See the header of `web/src/api/setup.ts` for every route and its types. All
errors are `{"detail": {"title", "message", "hint", "problems"?}}`.

## MCP

`POST /v1/mcp` is a Model Context Protocol server (Streamable HTTP, JSON
responses) exposing `find`, `timeline`, `orient`, `sql`, `person` and
`describe`. Authenticate with an agent token (`Authorization: Bearer …`).
Each tool calls the matching HTTP route with the same token, so asker scopes,
budgets and the audit log apply unchanged.
