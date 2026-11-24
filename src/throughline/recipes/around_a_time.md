---
{
  "name": "around_a_time",
  "version": 2,
  "schema_version": "0052",
  "status": "tested",
  "summary": "Everything around a moment across channels, messages and transcripts alike, by effective time, closest first.",
  "needs": [
    "function:around",
    "function:day"
  ],
  "statements": [
    "SELECT a.message_id, a.channel, a.kind, a.effective_time, a.offset_s, a.is_me, a.ignored FROM api.around('2020-01-01T00:03:00Z', 1800, NULL, 50) a"
  ],
  "expect": {
    "min_rows": 2,
    "columns": [
      "message_id",
      "channel",
      "effective_time",
      "offset_s"
    ]
  }
}
---

# Around a time

`api.around(at, window_s, persons, max_results)` returns records of every channel you may read (chats, transcripts, plays, visits) within window_s seconds of a time, by effective time, closest first, with offset_s. `api.day(date, tz)` returns one local day in time order.

## Adapting

- Pass `persons` to keep only records by those people.
- Transcript turns use the effective time from annotations (an owner correction of a wrong recording clock) and recordings are never split.
- Ignored and media-noise turns are excluded unless `include_ignored => true`.

## Troubleshooting

- Record time versus subject time: around uses effective (record) time; a page revision about 2019 written in 2024 is around 2024. Use recall with time_basis subject for "about then".
