# throughline-companion

The desktop half of a Throughline install, for Windows, macOS and Linux:

- runs the connectors that must read this computer (Claude Code, Codex,
  Cursor, Aider, T3 Code, LM Studio, browser history, ActivityWatch, Markdown
  notes, and on macOS Apple Notes and iMessage) on their schedules, each with
  its own plugin token;
- can download, verify and supervise ActivityWatch;
- answers bridge requests from connectors on the server that need something
  only the desktop has (browser cookies of allow-listed sites, files in
  shared folders, named localhost services);
- has a tray icon and a local dashboard (127.0.0.1 only, session key).

The same scheduler runs the server connectors and maintenance jobs in Docker
(`run --headless`).

```sh
uv run throughline-companion run          # tray + dashboard
uv run throughline-companion pair tlc1... # pairing code from `throughline lib companion enroll`
uv run throughline-companion detect       # which desktop connectors have data here
uv run throughline-companion --help
```

See [docs/companion.md](../docs/companion.md) for setup, the bridge's
security model and packaging.
