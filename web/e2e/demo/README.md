# Onboarding walkthrough (real server, recorded)

`walkthrough.spec.ts` drives the whole onboarding wizard against a real,
fresh Throughline with invented data and records a video. Nothing is mocked.

```sh
# 1. invented data: 18 months of Google Timeline, an IMAP mailbox, Beeper and Wave recordings
uv run python web/e2e/demo/make_demo_data.py "$DEMO/data"

# 2. a fresh database and a server whose setup files live in $DEMO
createdb tl_onboarding_demo
THROUGHLINE_DEV=1 THROUGHLINE_DATABASE_URL=postgresql://postgres@127.0.0.1:54331/tl_onboarding_demo uv run throughline migrate
THROUGHLINE_DEV=1 THROUGHLINE_DATABASE_URL=postgresql://postgres@127.0.0.1:54331/tl_onboarding_demo \
  THROUGHLINE_SETUP_DIR="$DEMO/config" THROUGHLINE_SETUP_DATA="$DEMO/setup" \
  uv run throughline serve --port 8811

# 3. build the UI and record (video under test-results/demo)
cd web && pnpm build
DEMO_DIR="$DEMO" npx playwright test -c playwright.demo.config.ts walkthrough
```

`FROM=<step>` resumes at a later step (it signs in with the account a full run
created), which is handy while iterating. The demo connectors read their
invented recordings through test settings (`IMAP_FIXTURE`,
`WAVE_API_FIXTURE`, `BEEPER_RECORDING`) that the spec sets through the API;
everything else is typed into the wizard. Only pick the demo sources: the
Import step imports exactly what was selected, and local-machine plugins
(coding-agent logs, browser history) would read this computer's real data.

The Lenses and Pages steps call a real model. The walkthrough picks the "CLI
proxy" provider for the Librarian role and types the endpoint in
`DEMO_MODEL_URL` (default `http://127.0.0.1:8317/v1`) and the model in
`DEMO_MODEL` (default `gpt-6-luna`); point them at any OpenAI-compatible
endpoint you have. Start the server with `THROUGHLINE_PROXY_API_KEY` in its
environment (the wizard then shows the key as set by the server, so it is
never typed or recorded).
Onboarding copies that key into `$DEMO/config/secrets.json` (as the lens/page
model key): delete that file after recording.
