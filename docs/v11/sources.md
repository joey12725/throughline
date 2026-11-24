# Source health: outages, stalls and catch-up

Migration `0057_source_health.sql`, modules `throughline.health` and
`throughline.cadence`, routes in `api/routes/sources.py`, command
`throughline lib sources`.

Every data source goes offline sometimes: a token expires, a machine sleeps, a
timer is disabled, an upstream feed changes. This component makes it obvious
when a source is down or stalled, records each outage, and shows that the
source caught back up when it returned.

## One look

```sh
throughline lib sources
```

```text
SOURCE        STATE    LAST SUCCESS      AGO      NOTE
email         DOWN     2026-11-19 04:40  1d 11h   no run has reported for 1d 11h (expected every 15m): is its timer or service running?
youtube_feed  STALLED  2026-11-20 16:05  12m      runs succeed, but youtube.uploads: no new item for 2d 3h; a silence this long is under 1 in a billion for its usual pattern; overdue since Wed Nov 18 22:29; busiest Mon 12h, Wed 10h, Sat 10h
calendar      FAILING  2026-11-20 15:40  37m      last run failed (1 in a row), data still recent: HTTP 503
t3code        ok       2026-11-20 16:06  9m       restored 2h ago after 3d 4h down; caught up 412 messages

1 down, 1 stalled, 1 failing, 1 ok
```

It reads the database directly (owner connection), so it answers when the
API server is down too. `--json` gives the full structure.
`GET /v1/source-health` (owner or agent) returns the same, and `GET /v1/status`
carries the summary under `staleness.sources`.

## States

Two independent signals decide the state.

**Runs.** Every plugin run ends with a heartbeat: success, or an error (the
SDK sends the redacted error text).

**Arrivals.** For each channel, the hours of the week in which new items
usually appear, and how unlikely the current silence is (see
[cadence](#arrival-cadence)). This catches a source whose runs succeed while
nothing new arrives: a feed that only serves its newest items, an expired
session answering with an empty page, an upstream job that stopped.

| state | meaning |
| --- | --- |
| `ok` | runs succeed and every channel's silence is ordinary |
| `failing` | the last run failed, but the data is still recent (fewer than `THROUGHLINE_SOURCE_DOWN_AFTER` failures in a row, default 3) |
| `stalled` | runs succeed (or fail only briefly), but a channel is **overdue**: a silence this long is very unlikely given its usual pattern |
| `down` | no successful run within twice the interval, or that many failures in a row. Kind `silent`: no run reported at all (the timer, service or importer stopped). Kind `failing`: runs keep failing |
| `never` | registered (or only dry-run) but never ran; shown, not alerted. A source that ran and failed is `down` |
| `paused` | switched off on purpose; shown, never alerted |

`down` and `stalled` are problems: `lib sources check` exits 2 and alerts.

## Outages and catch-up

`tl.source_outages` keeps one row per outage. An outage opens when a source
becomes down or stalled: on a failed heartbeat, or when a read or `check`
notices a silent outage or a stall by the clock. It records:

- `last_good_at`: when the data was last known good (the last successful run,
  or for a stall the newest item of the overdue channel),
- `detected_at`, `kind` (`failing`, `silent`, `stalled`), the first and last
  error seen, the number of failures.

It closes when the source is healthy again: runs succeed **and** no channel
is overdue. A successful run that brings nothing new does not end a stall.
On closing it records:

- `restored_at`,
- `caught_up_messages`: the messages that now fill the window since
  `last_good_at`,
- `caught_up_to`: the newest item now held,
- `catch_up`: `caught_up`, or `partial` with `catch_up_reasons` when the
  catch-up runs reported partial coverage (`source_cap`, `rate_limited`, ...)
  or left coverage gaps open in the outage window.

`throughline lib sources outages [NAME]` lists them, and so does
`GET /v1/source-health/outages?plugin=&limit=`.

### Why the catch-up is automatic

Plugins resume from their stored cursor, which only moves at checkpoints
written after the data is ingested. Nothing about an outage changes that
cursor, so the first good run after one ingests everything the source still
holds, however long the outage was. The SDK supports this in three ways:

- `sync` without `--once` never exits on a failed run. It sends a failure
  heartbeat and retries after 1, 2, 4 ... minutes, capped at the plugin's
  interval, then returns to the normal interval after a success. Under a
  timer (`sync --once`), a failed run exits 1 and the next tick retries.
- Before `records()` the runner reads `GET /v1/plugins/{name}/health` into
  `self.health`. `self.catch_up_since(timedelta(days=N))` widens a fixed "last
  N days" lookback to the last successful run.
- A failure heartbeat keeps the plugin's interval (it used to reset it to the
  default).

What cannot be caught up is shown, not hidden. A source that only serves its
newest items (a recently-played list of 50, an RSS feed of 15) loses whatever
fell off during a long outage. Such a plugin reports the window as a coverage
gap with reason `source_cap`, the outage closes as `partial`, and recall over
that window says the source is not complete there (docs/v11/coverage.md). See
"Outages and catch-up" in docs/writing-a-plugin.md for what a plugin must do.

## Arrival cadence

`throughline.cadence` judges, for each non-reference channel, whether the
silence since its newest item is unusual.

1. **History.** The 12 weeks before the channel's newest item (`happened_at`,
   never in the future), as a set of active hours. Counting active hours
   rather than items keeps bursts (a conversation, a batch upload) from
   dominating.
2. **Weekly grid.** Each of the 168 hours of the week gets p = the share of
   its occurrences that had at least one item. Sparse slots borrow strength:
   p(slot) is shrunk toward p(hour of day), which is shrunk toward the
   channel's overall rate (2 pseudo-occurrences each). p is capped at 0.95,
   so one missed hour is never proof.
3. **Clock.** The grid is fitted in the owner's zone (`THROUGHLINE_TIMEZONE`,
   else `TZ`, else the system zone from /etc/localtime, always with its
   daylight-saving rules) and in UTC, and the one that explains the history
   better is kept. People follow local time, while scheduled jobs often run on
   UTC, whose local hour moves with daylight saving.
4. **Delivery delay.** The silence is measured up to now minus the plugin's
   interval and the channel's delivery lag. The lag is how old the newest item
   of each recent import run was, taking the lower quartile over the last ten
   runs, so one bulk import of old history does not inflate it. With fewer
   than three runs the lag cannot be told apart from the silence, and it is
   taken as zero.
5. **Score.** P(silence) is the product of (1 - p) over the hours of the
   silence, treating hours as independent. It is small when the silence covers
   many hours in which the channel is normally active: three missed weekday
   mornings of a daily digest, or an evening without a message on a chat that
   is busy every evening. A quiet weekend on a work channel stays likely.
6. **Calibration.** Hours are not really independent for people (a whole
   quiet day, a holiday), so the product alone overstates how rare a quiet
   stretch is. Every past silence in the history is scored the same way, and
   the current one counts as rare only when it is rarer than all of them. A
   chat used on half of all days does not alarm after two quiet evenings.
7. **Longest silence.** A second, model-free test compares the silence with
   the longest one in the history (given at least 4 weeks and 20 gaps of
   history). It catches regular sources whose items jitter across neighbouring
   hours.

| verdict | when |
| --- | --- |
| `no_data` | no items yet |
| `learning` | under 14 days or 12 active hours of history |
| `normal` | none of the below |
| `quiet` | rarer than every past silence and P below `THROUGHLINE_CADENCE_QUIET_P` (0.01), or longer than the longest past silence: shown, not alerted |
| `overdue` | rarer than every past silence and P below `THROUGHLINE_CADENCE_OVERDUE_P` (0.001), or twice the longest past silence: the source is **stalled** |

Every verdict carries `overdue_at`: when the silence became overdue, or will
if nothing arrives. `throughline lib sources cadence [NAME]` shows each
channel:

```text
youtube.uploads  overdue  no new item for 2d 3h; a silence this long is under 1 in a billion for its usual pattern; overdue since Wed Nov 18 22:29; busiest Mon 12h, Wed 10h, Sat 10h
weather.daily    quiet    no new item for 2d 13h; a silence this long is 1 in 46 for its usual pattern; overdue at Fri Nov 20 20:10 if nothing arrives; busiest Thu 01h, Fri 01h, Sat 01h
t3code.messages  normal   no new item for 4m; would be overdue at Mon Nov 23 19:55; busiest Wed 11h, Tue 20h, Wed 12h
```

A channel that has really ended (a sub-feed nobody publishes to any more)
would stay overdue for good. Pause it: `throughline lib sources pause
weather.alerts --reason "feed retired"` (`tl.channel_pauses`). A paused channel
is still shown, but never stalls its source.

## The alarm

```sh
throughline lib sources check            # exit 0 all well, 2 when a source is down, stalled or never ran
```

With `THROUGHLINE_ALERT_CMD` set, `check` runs it once when a source goes
down or stalls, and once when it is back (with the catch-up summary). The
state last alerted is kept in `tl.plugins.alerted_state`, so a check every few
minutes does not repeat an alert. A failed alert command is retried by the
next check, and without a command, pending alerts are listed and kept for
when one is configured. The sweep is committed before any command runs (so
heartbeats never wait on an alert), and an advisory lock lets only one check
send alerts at a time. The template runs without a shell, and the
placeholders `{plugin}`, `{state}`, `{title}` and `{message}` are filled
inside its arguments:

```sh
# desktop notification
THROUGHLINE_ALERT_CMD='notify-send {title} {message}'
# phone push through ntfy
THROUGHLINE_ALERT_CMD='curl -s -H "Title: {title}" -d {message} https://ntfy.sh/your-private-topic'
```

Run it every 15 minutes, for example as a systemd user timer:

```ini
# ~/.config/systemd/user/throughline-sources.service
[Service]
Type=oneshot
EnvironmentFile=%h/.throughline/alert.env
ExecStart=/usr/bin/env bash -c 'source ~/.throughline/env.sh && throughline lib sources check --quiet; test $? -ne 1'

# ~/.config/systemd/user/throughline-sources.timer
[Timer]
OnCalendar=*:0/15
Persistent=true
[Install]
WantedBy=timers.target
```

## Pausing

`throughline lib sources pause NAME [--reason ...]` takes a source (plugin)
or a channel name, and `resume NAME` undoes it. Over HTTP (owner only), use
`POST /v1/source-health/{name}/pause {reason?}` and
`POST /v1/source-health/{name}/resume`.

## Settings

| variable | default | meaning |
| --- | --- | --- |
| `THROUGHLINE_INTERVAL_S` | manifest default | SDK: seconds between runs, sent with each heartbeat. Set it when a timer runs `sync --once` on its own schedule: a source is down after two intervals without success |
| `THROUGHLINE_SOURCE_DOWN_AFTER` | 3 | failures in a row that make a source down while its data is still recent |
| `THROUGHLINE_CADENCE_QUIET_P` | 0.01 | silence probability below which a channel is quiet |
| `THROUGHLINE_CADENCE_OVERDUE_P` | 0.001 | silence probability below which a channel is overdue |
| `THROUGHLINE_TIMEZONE` | `TZ`, then /etc/localtime | the owner's clock for day-of-week and hour |
| `THROUGHLINE_ALERT_CMD` | unset | alert command template |

## Access

Every route uses the app role after `require_unrestricted`. Plugins, like the
people tables, are not readable by a restricted asker. `GET /v1/source-health`
and `/outages` need an owner or agent token, pause and resume need an owner
token, and `GET /v1/plugins/{name}/health` needs the plugin's own token (or an
owner token). The outage table holds no content: only plugin names, times,
counts and the redacted error text the SDK sends (at most 500 characters).
