# throughline-capture

Uploads what the Throughline Companion's optional screen and audio capture
records: screenshots to Immich (`capture.screens` records carry the asset
id), transcripts of speech to `capture.transcripts`. Runs in the Companion on
the same desktop; see [docs/capture.md](../../docs/capture.md).

Settings: `CAPTURE_SPOOL_DIR` (set by the Companion), `IMMICH_URL`,
`IMMICH_API_KEY` (an Immich key with only `asset.upload`),
`CAPTURE_DELETE_AFTER_UPLOAD` (default true).
