# Plugin onboarding contract

A plugin repository tells Throughline how to show, configure, install,
check and backfill its plugins with a small TOML file. Throughline reads it
**before running any code from the repository**, so the onboarding screens
and setup agents can explain a source, ask for exactly the right
credentials, and refuse a broken repository with a clear message.

This contract sits beside the two runtime contracts and does not replace
them:

| Contract | What it covers |
|---|---|
| [HTTP contract](plugin-http-contract.md) | How a running plugin talks to the core |
| [Writing a plugin](writing-a-plugin.md) | The Python SDK, `Manifest`, records, checkpoints |
| **Onboarding contract** (this page) | How a repository is listed, installed and set up |

## Repository layouts

**Several plugins in one repository** (the starter plugins work this way):
put `throughline-plugins.toml` at the root.

```toml
schema = 1
name = "My plugins"
description = "Connectors for the services I use."

[[plugins]]
path = "wave"            # wave/throughline-plugin.toml holds the entry

[[plugins]]              # or the whole entry inline
id = "letterboxd"
path = "letterboxd"
name = "Letterboxd"
# ...the same keys as throughline-plugin.toml
```

**One plugin per repository**: put `throughline-plugin.toml` at the root.

Anyone can install either kind from the onboarding **Add from GitHub** box or
Settings → Plugins by pasting `https://github.com/owner/repo` (or
`owner/repo`, or `…/tree/branch`). Throughline clones it with
`git clone --depth 1`, validates every entry, and lists the plugins it
found. Nothing is installed until the owner picks a plugin.

## A plugin entry

```toml
id = "wave"                     # = Manifest.name (lowercase, digits, _)
name = "Wave"
summary = "Meeting recordings and transcripts from Wave."
description = "Longer text shown on the setup page (Markdown)."
category = "meetings"           # see the list below
icon = "audio-lines"            # a lucide icon name
homepage = "https://wave.co"
channels = ["recordings.wave", "transcripts.wave"]
track_sources = []              # location plugins: the track source names
platforms = ["any"]             # any | linux | macos | windows
requires = []                   # companion | docker | upload | network
pairs_with = ["calendar"]
check = true                    # the plugin implements `check`
starter = true                  # offered by default in onboarding

[install]
kind = "python"                 # python | command
entry = "throughline-wave"      # console script (python)
# run = ["node", "plugin.mjs"]  # argv relative to the plugin dir (command)

[schedule]
interval_s = 900
mode = "poll"                   # poll | webhook | stream | manual

[backfill]
supported = true
history = "full"                # full | window | none
note = "Imports every recording still on your Wave account."

[[setup.steps]]
title = "Create an API token"
body = "Open Wave → Settings → Developer and create a token."
url = "https://app.wave.co/settings"

[[setup.fields]]
key = "WAVE_TOKEN"              # the environment variable the plugin reads
label = "API token"
kind = "secret"
required = true
help = "Starts with wv_."
placeholder = "wv_…"
```

### Fields

| Key | Meaning |
|---|---|
| `key` | Environment variable the plugin reads; must match its `Manifest` declaration |
| `label`, `help`, `help_url`, `placeholder` | What the form shows |
| `kind` | `text`, `secret`, `url`, `email`, `path`, `directory`, `file`, `select`, `boolean`, `number`, `list` (comma separated), `textarea` |
| `required` | The plugin cannot run without it |
| `default`, `options` | Default value; choices for `select` |
| `advanced` | Hidden behind "Advanced" |
| `when` | Show only when another field has one of the listed values, e.g. `when = { WAVE_SOURCE = ["api"] }` |

Only list the fields a typical owner needs. Every other `Manifest`
setting stays available under Advanced once the plugin is installed, with
its description from the manifest.

A `secret` is stored encrypted at rest in the owner's secret store, passed to
the plugin only as an environment variable, never returned by the API, and
redacted from every log line and error message. A `file` is uploaded by the
browser to the server's import area and its path is passed to the plugin
(Google Takeout exports work this way).

### Categories

`messages`, `email`, `calendar`, `meetings`, `location`, `health`, `media`,
`notes`, `ai`, `dev`, `social`, `finance`, `activity`, `photos`, `migration`,
`other`.

### Detection

When Throughline already holds data in a plugin's `channels` (a channel
pattern ending in `.*` matches a family) or `track_sources`, onboarding
marks the plugin **Already connected** and skips its setup. This is how a
restored backup or a migrated install picks up where it was.

## Runtime commands

Besides `manifest`, `backfill` and `sync` from the HTTP contract, a plugin
may implement:

- **`check`**: verify the configuration without importing anything (reach the
  service, authenticate, read one item). Print one JSON line
  `{"ok": true, "message": "Signed in as …"}` and exit 0, or
  `{"ok": false, "message": "The token was refused (401). Create a new one at …"}`
  and exit 1. Write messages for a person: what went wrong and what to do.
  Python plugins get this from the SDK: override `Plugin.check()` and raise
  with a clear message on failure.
- **Progress lines**: when `THROUGHLINE_PROGRESS` is set, write
  `TL-PROGRESS {"delivered": 1200, "oldest": "2021-03-01T…", "newest": "…",
  "done": 40, "total": 300, "unit": "recordings"}` lines (at most about one a
  second). The SDK writes them automatically for every batch; call
  `self.progress(done=, total=, unit=, note=)` when you know the total.

## Validation

`throughline lib plugins validate PATH` checks a checkout and prints every
problem with its location. The same checks run when a repository is added
from the web UI, and a repository with any invalid entry is refused as a
whole, listing all problems at once.
