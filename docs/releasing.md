# Releasing

For maintainers. The public repository is not a mirror of a development
repository: it is built from an allowlisted tree and starts as **one commit
by a neutral author**. Never `git push --mirror` a development clone into
it; local refs, stashes and checkpoints can hold data that was never meant
to be published.

## 1. Export

From an up-to-date, committed development checkout:

```sh
scripts/export-public.sh --repo OWNER/throughline /path/to/throughline-public
```

The script:

- takes only **committed** content of `HEAD` (or `--ref REF`) through
  `git archive`: the working tree, untracked files and other refs are never
  read;
- stops if `.gitmodules` is still present (plugins must be in-tree) and if a
  top-level entry is neither in its allowlist nor in
  [`scripts/export-denylist.txt`](../scripts/export-denylist.txt), then leaves
  out every denylisted path (private working files, private connectors,
  secrets that should never have been committed, scratch files);
- replaces the repository-name placeholder (`GITHUB_REPO` between `@`
  signs) in the docs with `--repo`;
- refreshes `uv.lock` (without upgrades) when left-out workspace members
  made it stale;
- creates a fresh git repository on `main` with one commit by
  `Throughline contributors <noreply@throughline.invalid>`, with global and
  system git configuration ignored, no hooks and no signing;
- checks the result: one commit and one ref, no remote, no `.gitmodules`,
  [`scripts/leak-check.sh`](../scripts/leak-check.sh), no e-mail address
  from the development history anywhere in the files, `gitleaks` and
  `trufflehog` when they are installed, and
  [`scripts/check-links.py`](../scripts/check-links.py).

It ends by printing the one command that would publish the export. It never
runs it.

## 2. Inspect

Read the export before publishing; the checks are a floor, not a review.

```sh
cd /path/to/throughline-public
git show --stat HEAD | less
git log --format='%an <%ae>%n%cn <%ce>'     # the neutral author only
scripts/leak-check.sh
gitleaks detect --source .                    # if not run by the export
```

Then build and test it as a newcomer would, from that directory:

```sh
THROUGHLINE_DEV=1 uv sync --frozen --all-packages && uv run --frozen ruff check .
THROUGHLINE_DEV=1 uv run --frozen pytest -q -n auto --dist loadfile
(cd web && pnpm install --frozen-lockfile && pnpm typecheck && pnpm test && pnpm build)
docker build -t throughline:release-check .
```

and follow the README quickstart verbatim (`scripts/install.sh`, seed the
twin, the `/v1/find` call and the restricted agent token), with
`COMPOSE_PROJECT_NAME` set to something unused and `--port` set to a free
port; `docker compose down -v` afterwards. Scan the image too, for example
`trufflehog docker --image throughline:release-check`.

## 3. Publish

The export prints the command; it is a single step:

```sh
gh repo create OWNER/throughline --public --source /path/to/throughline-public --push
```

This creates the repository and pushes `main`. It cannot be undone in any
useful way (forks and caches keep what was pushed), so run it only after
step 2.

## 4. Tag v0.1.0

In the published repository, once CI on `main` is green:

```sh
git tag -a v0.1.0 -m "Throughline 0.1.0"
git push origin v0.1.0
gh release create v0.1.0 --title "Throughline 0.1.0" --notes-file <(sed -n '/^## \[0.1.0\]/,/^## \[/p' CHANGELOG.md | sed '$d')
```

The Companion workflow builds the desktop apps on every push to `main`;
attach its artifacts to the release.

## 5. Protect `main`

In **Settings → Branches** (or a ruleset) for `main`:

- require a pull request before merging, with at least one approval;
- require the status checks `Python (core-1)`, `Python (core-2)`,
  `Python (plugins-1)`, `Python (plugins-2)`, `Web (typecheck, vitest, build)`,
  `Leak guard and links` and `Docker image` to pass;
- require linear history; block force pushes and deletion;
- include administrators.

Also enable **private vulnerability reporting** (Settings → Security; the
Security policy and the Code of Conduct point to it), Dependabot alerts and
security updates, and secret scanning with push protection.

## Later releases

Development continues in the development repository. For each release,
export again into a fresh directory, then copy the result over a clone of
the public repository and commit it there as one reviewed commit (or open
it as a pull request), so the public history only ever contains reviewed
exports. Bump the version in `pyproject.toml` and move the "Unreleased"
section of `CHANGELOG.md` first.
