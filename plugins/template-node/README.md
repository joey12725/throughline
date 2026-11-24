# Node template
Node 24, no npm dependencies. Run `node plugins/template-node/plugin.mjs backfill`
with TEMPLATE_INPUT, THROUGHLINE_URL and THROUGHLINE_TOKEN. Same JSONL as Python.

## License

This template is licensed under the Apache License 2.0 (see [LICENSE](LICENSE)), not the
GPL that covers Throughline itself, so a plugin started from it can use any
license you choose: plugins talk to Throughline only over HTTP (the Python
SDK they may use is permissively licensed too).
