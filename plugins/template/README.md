# JSONL template
Run `TEMPLATE_INPUT` pointing at recorded fake JSONL events with keys `id`, `at`
(offset timestamp), and `text`. Run `uv run throughline-template backfill`,
`backfill --dry-run`, or `sync --once`. See ../../docs/writing-a-plugin.md.

## License

This template is licensed under the Apache License 2.0 (see [LICENSE](LICENSE)), not the
GPL that covers Throughline itself, so a plugin started from it can use any
license you choose: plugins talk to Throughline only over HTTP (the Python
SDK they may use is permissively licensed too).
