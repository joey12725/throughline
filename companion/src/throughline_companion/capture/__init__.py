"""Screen and audio capture (optional, off by default; docs/capture.md).

The engine writes to a local spool only: screenshots with their window
context, and transcripts of speech. Nothing is kept on the desktop after
upload: the `capture` connector uploads screenshots to Immich, ingests the
records into Throughline and deletes the spool files. Audio is never kept;
each speech segment is deleted once transcribed.
"""
