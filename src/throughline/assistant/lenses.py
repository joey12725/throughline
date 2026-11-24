"""The lens catalogue as the helper's model reads it: one place to update.

Mirrors LENS_CATALOG in web/src/lens/lenses/index.ts (ids, groups, one-line
summaries and encode channels) and the shape of VIEW_SPEC_SCHEMA
(throughline.view/1) in web/src/lens/types.ts. When a lens is added or its
channels change there, change LENSES here; tests/test_web_assistant.py checks
that every id below is a lens id of the front end.
"""

# id: (group, summary, {encode channel: what it binds}, draws any SQL result)
LENSES = {
    "spine": (
        "time",
        "Records per time bucket over the whole life of the data; the axis every brush starts from.",
        {"x": "bucket start (timestamp)", "y": "records in the bucket"},
        True,
    ),
    "timeseries": (
        "time",
        "A quantity (or a count per category) per time bucket.",
        {
            "x": "bucket start (timestamp)",
            "y": "value column, or several value columns drawn as separate lines",
            "lo": "optional lower band (minimum in the bucket)",
            "hi": "optional upper band (maximum in the bucket)",
            "series": "optional category that splits the line",
        },
        True,
    ),
    "small_multiples": (
        "time",
        "The same small time chart repeated per family member, per group or per sibling channel.",
        {
            "x": "bucket start (timestamp)",
            "y": "value column; a list of columns makes one panel per column",
            "facet": "optional column whose values make the panels",
        },
        True,
    ),
    "calendar_heat": (
        "time",
        "One cell per local day, coloured by record count or by a daily value.",
        {"date": "local calendar day (date)", "value": "count or value of the day"},
        True,
    ),
    "weekday_hour": (
        "time",
        "A 7 by 24 grid of record counts: weekday against hour of day, in the viewer's zone.",
        {
            "weekday": "ISO weekday 1 (Monday) to 7",
            "hour": "hour of day 0 to 23",
            "value": "records in the cell",
        },
        True,
    ),
    "interval_clock": (
        "time",
        "Date against time of day; each record a bar from its start to its end.",
        {
            "start": "interval start (timestamp)",
            "end": "interval end (timestamp)",
            "color": "optional category",
        },
        True,
    ),
    "gantt": (
        "time",
        "Labelled bars on a time axis, in lanes; for intervals that last hours or days.",
        {
            "start": "interval start",
            "end": "interval end",
            "label": "bar label",
            "lane": "optional lane category",
        },
        True,
    ),
    "sawtooth": (
        "time",
        "A level against time, stepped, one line per group, with resets as vertical drops and the next reset marked.",
        {
            "x": "time",
            "y": "level",
            "series": "optional group key",
            "marker": "optional time still to come (the next reset)",
        },
        False,
    ),
    "histogram": (
        "distribution",
        "How the values of one quantity are distributed, in equal bins.",
        {"x0": "lower bin edge", "x1": "upper bin edge", "y": "records in the bin"},
        True,
    ),
    "top_values": (
        "distribution",
        "The most common values of one field as ranked bars.",
        {
            "label": "the value",
            "value": "records with that value (or the measure of that row)",
        },
        True,
    ),
    "composition": (
        "distribution",
        "Stacked parts of a whole per time bucket.",
        {
            "x": "bucket start (timestamp)",
            "parts": "the part columns, stacked in this order",
            "whole": "optional column holding the total",
        },
        False,
    ),
    "scatter": (
        "distribution",
        "One dot per record: one quantity against another.",
        {
            "x": "horizontal quantity",
            "y": "vertical quantity",
            "color": "optional category",
        },
        True,
    ),
    "strip": (
        "distribution",
        "Every record as a tick on one bounded axis, coloured by a category: where the decisions fall.",
        {"x": "the bounded quantity", "color": "optional category"},
        True,
    ),
    "thread_tapestry": (
        "structure",
        "One row per thread, one mark per time bucket: which conversations lived when, and how busy they were.",
        {
            "row": "thread id",
            "label": "thread title",
            "x": "bucket start (timestamp)",
            "value": "records of the thread in the bucket",
            "people": "distinct speakers in the bucket",
            "order": "the thread's first activity, for sorting rows",
        },
        False,
    ),
    "speaker_share": (
        "structure",
        "Who carries the channel: records and characters per speaker.",
        {"label": "speaker", "value": "records", "chars": "characters written"},
        False,
    ),
    "reply_latency": (
        "structure",
        "How long a reply takes: the gap between consecutive records of a thread when the speaker changes.",
        {
            "bin": "latency class, 0 (under a minute) to 7 (over a week); labels in options.bins",
            "value": "replies in the class",
        },
        False,
    ),
    "status_matrix": (
        "structure",
        "Rows of sources against days, each cell coloured by status: where and when things failed.",
        {
            "x": "bucket start (timestamp)",
            "lane": "row label",
            "color": "status",
            "value": "records in the cell",
        },
        True,
    ),
    "tags": (
        "structure",
        "The most common elements across a list-valued field.",
        {"label": "the element", "value": "records that carry it"},
        False,
    ),
    "json_tree": (
        "structure",
        "The structure of a JSON field, merged over the latest records: keys, types and example values.",
        {"value": "the JSON value", "id": "record id", "time": "record time"},
        True,
    ),
    "track_map": (
        "space",
        "The positions joined into a line in time order, coloured by time of day.",
        {
            "t": "time",
            "lat": "latitude",
            "lon": "longitude",
            "accuracy": "optional accuracy in metres",
        },
        True,
    ),
    "pin_map": (
        "space",
        "Places as circles sized by how many records happened there.",
        {
            "lat": "latitude",
            "lon": "longitude",
            "size": "records at the place",
            "label": "optional place name",
        },
        True,
    ),
    "dwell_rank": (
        "space",
        "Places ranked by the time spent there.",
        {
            "label": "place",
            "value": "total time in seconds",
            "visits": "stays",
            "lat": "latitude",
            "lon": "longitude",
        },
        False,
    ),
    "ego_graph": (
        "relation",
        "The owner at the centre and the people of this channel around: size by volume, distance by recency.",
        {
            "node": "person id",
            "label": "person name",
            "weight": "records",
            "threads": "threads shared",
            "recency": "last record time",
        },
        False,
    ),
    "link_matrix": (
        "relation",
        "A grid of one grouping against another, each cell the number of records the two share.",
        {"row": "row label", "col": "column label", "value": "records shared"},
        True,
    ),
    "reader": (
        "reading",
        "The records themselves in the form their archetype reads best: bubbles, transcript, documents, feed or cards.",
        {
            "id": "record id",
            "time": "record time",
            "text": "content (clipped; the drawer loads the whole record)",
            "author": "resolved author",
            "thread": "thread id",
            "title": "optional title field",
            "speaker": "optional speaker field",
            "url": "optional link field (inert until clicked)",
            "actor": "optional publisher or organiser field",
            "status": "optional status field",
            "place": "optional place field",
            "end": "optional end time",
            "start_offset": "optional start offset in seconds",
            "end_offset": "optional end offset in seconds",
        },
        True,
    ),
    "table": (
        "reading",
        "Rows and columns, cells rendered by role.",
        {"columns": "the columns to show, in order"},
        True,
    ),
    "stat": (
        "reading",
        "A few large numbers: totals over the scope, or the latest values.",
        {
            "values": "the columns to show as numbers, in order",
            "time": "optional time the values are from",
        },
        True,
    ),
}

# Parameter types a spec's source.params may declare.
PARAM_TYPES = (
    "text",
    "int",
    "number",
    "bool",
    "timestamp",
    "date",
    "interval",
    "text[]",
    "int[]",
    "number[]",
    "grain",
    "tz",
)

SPEC_FORMAT = """View spec (throughline.view/1), a JSON object with exactly these keys:
  v: 1
  title: short title (1..160 chars)
  why: one sentence, why this picture for this data (<= 400 chars)
  lens: one lens id from the list below
  source: {sql: the statement (one SELECT), row_cap: 1..10000}
  encode: {<encode channel of the lens>: <result column> or [<columns>]}
  options: optional renderer hints that are not columns (units, value domain)
  drill: optional {to: "reader" | "record", key: <id column>}
  provenance: {by: "agent", sampled: false, statement: "as_is"}
Use only channels the lens lists and only columns the statement returns.
Prefer lenses marked [any result]: they draw an arbitrary SQL result."""


def lens_ids():
    return tuple(LENSES)


def prompt_text():
    """The compact description of every lens for the system prompt."""
    lines = [SPEC_FORMAT, "", "Lenses (id [group]: summary; encode channels):"]
    for lens, (group, summary, channels, results) in LENSES.items():
        bound = "; ".join(f"{k}={v}" for k, v in channels.items())
        flag = " [any result]" if results else ""
        lines.append(f"- {lens} [{group}]{flag}: {summary} Channels: {bound}")
    return "\n".join(lines)
