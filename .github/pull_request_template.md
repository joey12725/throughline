## What and why

<!-- The behaviour change, and the problem it solves. Link the issue. -->

## How it was verified

<!-- Commands you ran and their result. Tests added or changed. -->

## Checklist

- [ ] Tests, fixtures, screenshots and examples use **invented data only** (no real names, messages, addresses, locations, hosts or tokens).
- [ ] `uv run ruff check .` and `THROUGHLINE_DEV=1 uv run pytest -q` pass; web changes: `pnpm typecheck && pnpm test`.
- [ ] `scripts/leak-check.sh` and `python3 scripts/check-links.py` pass.
- [ ] New migrations are new files (forward-only; no released migration edited or renamed).
- [ ] New read paths go through the asker gate / `api.*` views and have a security-matrix case.
- [ ] Docs and `docs/configuration.md` updated; a line added to `CHANGELOG.md` under Unreleased.
