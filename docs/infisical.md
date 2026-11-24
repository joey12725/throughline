# Infisical secret integration

Throughline can link credentials directly from [Infisical](https://infisical.com) (Infisical Cloud or a self-hosted instance) so API keys, connector tokens, and passwords are fetched in memory at run time instead of being stored in `.env`, `throughline.env`, `connectors.env`, `companion.json`, or `docker/connectors.json`.

When no setting uses an `infisical:` reference, Throughline makes **zero** network calls to Infisical.

## 1. Connect Throughline to Infisical

Create a read-only **Machine Identity** in Infisical with **Universal Auth** (`Client ID` + `Client Secret`), grant it read access to your project and environment, and configure the connection in one of two ways:

### Environment variables (server, Docker Compose, CLI, standalone plugins)

With Docker Compose, place these in `infisical.env` next to `docker-compose.yml` (gitignored, automatically loaded by `migrate`, `throughline`, `connectors`, and `maintenance`):

```env
INFISICAL_SITE_URL=https://app.infisical.com
INFISICAL_CLIENT_ID=<machine-identity-client-id>
INFISICAL_CLIENT_SECRET=<machine-identity-client-secret>
INFISICAL_PROJECT_ID=<project-uuid>
INFISICAL_ENVIRONMENT=prod
INFISICAL_SECRET_PATH=/
```

Alternative authentication and optional settings:

| Variable | Default | Meaning |
|---|---|---|
| `INFISICAL_SITE_URL` (`INFISICAL_API_URL`) | `https://app.infisical.com` | Infisical Cloud or self-hosted base URL |
| `INFISICAL_CLIENT_ID` (`INFISICAL_UNIVERSAL_AUTH_CLIENT_ID`) | — | Machine Identity Universal Auth Client ID (recommended) |
| `INFISICAL_CLIENT_SECRET` (`INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET`) | — | Machine Identity Universal Auth Client Secret |
| `INFISICAL_TOKEN` | — | Direct Bearer token (alternative to Universal Auth) |
| `INFISICAL_PROJECT_ID` (`INFISICAL_WORKSPACE_ID`) | — | Target project UUID (recommended) |
| `INFISICAL_PROJECT_SLUG` (`INFISICAL_WORKSPACE_SLUG`) | — | Target project slug (used when Project ID is unset) |
| `INFISICAL_ENVIRONMENT` | `prod` | Default Infisical environment slug |
| `INFISICAL_SECRET_PATH` | `/` | Default folder path inside the environment |
| `INFISICAL_CACHE_TTL_S` | `300` | In-memory cache TTL in seconds for Throughline core (`0` disables caching; Companion jobs always fetch fresh per run) |
| `INFISICAL_TIMEOUT_S` | `15` | HTTP timeout in seconds |

### Throughline Companion (desktop app or `connectors.json`)

You can configure Infisical in the Companion dashboard under **Infisical**, via the CLI, or in `companion.json`:

```sh
throughline-companion infisical set \
  site_url=https://app.infisical.com \
  project_id=<project-uuid> \
  environment=prod \
  client_id=<client-id> \
  client_secret=env:INFISICAL_CLIENT_SECRET

throughline-companion infisical test
throughline-companion infisical list --recursive
```

Any field inside the `"infisical"` block of `companion.json` accepts `"env:VAR_NAME"`, so even your Infisical client secret can stay out of `companion.json`.

## 2. Link secrets by reference

### Reference formats

1. **String shorthand** (works everywhere — `throughline.env`, `.env`, `companion.json`, `docker/connectors.json`, CLI, standalone plugins):
   - `infisical:GITHUB_TOKEN` — secret `GITHUB_TOKEN` at the default project, environment, and path
   - `infisical:/connectors/github/GITHUB_TOKEN` — secret `GITHUB_TOKEN` inside folder `/connectors/github`
   - `infisical:GITHUB_TOKEN?env=staging&path=/connectors` — override environment, folder path, `project_id`, `project_slug`, or `as=file`

2. **Structured object** (in `companion.json` / `docker/connectors.json` plugin `env`, alongside desktop bridge references):
   ```json
   {
     "GITHUB_TOKEN": {
       "infisical": "GITHUB_TOKEN",
       "path": "/connectors/github",
       "environment": "prod"
     },
     "GOOGLE_APPLICATION_CREDENTIALS": {
       "infisical": "GOOGLE_SERVICE_ACCOUNT_JSON",
       "path": "/connectors/calendar",
       "as": "file"
     }
   }
   ```
   With `"as": "file"` (or `?as=file`), the runner writes the secret to a private `0600` temporary file for the duration of that job run and deletes it automatically when the job finishes.

### Where references work

- **Throughline core (`throughline.env` / `.env`)**:
  - Model and embedding keys: `THROUGHLINE_EMBEDDING_KEY`, `VOYAGE_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `OPENROUTER_API_KEY`, `THROUGHLINE_RERANK_KEY`, `THROUGHLINE_SEMANTIC_KEY`, `THROUGHLINE_CONTROLLER_KEY`, `THROUGHLINE_MODEL_KEY`, `THROUGHLINE_TABLE_LLM_KEY`, `OPENAI_API_KEY`, `THROUGHLINE_JEV_KEY`, and any custom `api_key_env` on a lens.
  - Database and CLI credentials: `THROUGHLINE_DATABASE_URL`, `THROUGHLINE_APP_PASSWORD`, `THROUGHLINE_READER_PASSWORD`, `PGPASSWORD`, `THROUGHLINE_TOKEN`.
- **Server connectors (`docker/connectors.json`) and desktop connectors (`companion.json`)**:
  - Any plugin `env` entry (direct string, `"env:NAME"` whose value is an `infisical:` ref, or structured `{"infisical": ...}` object).
  - Plugin `token` and command job `token`.
  - Companion dashboard: every plugin **Secret** field has a **Direct / Infisical** toggle with autocomplete of accessible Infisical secret names (names only; values are never fetched or shown in the UI).
- **Companion screen/audio capture**:
  - `capture.transcription.api_key` accepts `infisical:GROQ_API_KEY` (or `env:GROQ_API_KEY`).
- **Standalone plugins (`throughline-sdk`)**:
  - `Plugin.__init__` and `throughline_sdk.run` resolve `infisical:` references in declared settings, secrets, and `THROUGHLINE_TOKEN`, and add resolved secret values to the log redactor and payload leak guard automatically.

## 3. Verify your setup

Check configuration, connectivity, and every `infisical:` reference without ever printing secret values:

```sh
# Core / Docker
throughline lib infisical status
throughline lib infisical test
throughline lib infisical list --recursive
throughline lib infisical check --config docker/connectors.json

# Companion
throughline-companion infisical status
throughline-companion infisical test
throughline-companion infisical list --recursive
```

## Security properties

- **No stored credentials**: Only secret names/paths (such as `infisical:/connectors/github/GITHUB_TOKEN`) are saved in Throughline config files.
- **Per-plugin isolation**: The Companion runner strips `INFISICAL_*` variables from plugin subprocess environments, fetches only the specific secrets referenced by that plugin just before each run, and injects only those resolved values. A plugin never receives your Infisical Machine Identity credentials or another plugin's secrets.
- **Automatic redaction**: Every secret value resolved from Infisical is added to the Companion runner's and SDK's redactors so it cannot leak into logs, `last_error`, `last_output`, or failure heartbeats.
- **Safe browsing**: `list`, `status`, `test`, and `/api/infisical/secrets` discard `secretValue` immediately and return only key names, paths, and environments.
- **No redirect credential leaks**: Outbound requests to Infisical never follow HTTP redirects (`follow_redirects=False`).
