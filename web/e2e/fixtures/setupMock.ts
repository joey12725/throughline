/*
 * A stateful, in-memory mock of the whole onboarding API surface, driven through
 * Playwright's page.route(). It covers /v1/auth/* and /v1/setup/* plus the few
 * app routes the shell needs (/v1/web/bootstrap, /v1/auth/session, /v1/auth/config).
 * Job progress advances with wall-clock time so the import view animates, and a
 * couple of endpoints fail on purpose to exercise the SetupProblem rendering.
 *
 * No real backend is required; mocks intercept every request to this origin.
 */
import type { Page, Route } from "@playwright/test";

type Json = Record<string, unknown>;

interface MockOptions {
  /** no owner account yet → the sign-in gate shows account creation */
  needsAccount?: boolean;
  /** this install already holds data → restore/continue */
  existing?: boolean;
}

export function seedSetupMock(page: Page, opts: MockOptions = {}): void {
  const now = () => new Date().toISOString();
  const t0 = Date.now();

  /* ---- setup state ---- */
  const state: Json = {
    status: opts.existing ? "in_progress" : "fresh",
    mode: opts.existing ? "restore" : "fresh",
    steps: {},
    choices: { sources: ["wave", "gmail", "granola", "google-timeline", "beeper", "claude-code", "gbrain"] },
    owner_ready: true,
    owner_hint: null,
    started_at: now(),
    completed_at: null,
  };

  /* ---- detection ---- */
  const detection: Json = {
    existing: Boolean(opts.existing),
    totals: { records: opts.existing ? 128_400 : 0, people: opts.existing ? 420 : 0, channels: 11, plugins: 12, track_points: opts.existing ? 337_000 : 0 },
    sources: [
      { plugin_id: "oura", name: "Oura", connected: true, managed: true, records: 3650, first_at: "2019-02-01T00:00:00Z", last_at: now(), health: "ok" },
      { plugin_id: "wave", name: "Wave", connected: opts.existing, managed: false, records: opts.existing ? 54_200 : 0, first_at: "2016-06-01T00:00:00Z", last_at: now(), health: opts.existing ? "ok" : "never" },
    ],
    providers: { embedding: "voyage", assistant: null },
    companion_devices: 0,
    accounts: opts.needsAccount ? 0 : 1,
    restored_hint: Boolean(opts.existing),
  };

  /* ---- providers ---- */
  const providers: Json = {
    egress_allow: ["api.voyageai.com", "openrouter.ai", "api.openai.com"],
    roles: {
      embedding: { provider: "voyage", model: "voyage-3" },
      assistant: { provider: null, model: null },
      semantic: { provider: null, model: null },
    },
    providers: [
      {
        id: "voyage", name: "Voyage AI", tagline: "Best-in-class embeddings", roles: ["embedding"],
        configured: true, locked: [], docs_url: "https://docs.voyageai.com", cost_note: "~$0.02 / 1M tokens", privacy_note: "Text is sent to Voyage for embedding",
        values: { VOYAGE_API_KEY: "••••a1b2" },
        fields: [{ key: "VOYAGE_API_KEY", label: "API key", kind: "secret", required: true, placeholder: "pa-…", help: "From your Voyage dashboard", help_url: "https://dash.voyageai.com", options: [], default: null }],
        models: { embedding: [{ id: "voyage-3", label: "voyage-3", note: "1024-dim", default: true }, { id: "voyage-3-lite", label: "voyage-3-lite", note: "cheaper" }] },
      },
      {
        id: "openrouter", name: "OpenRouter", tagline: "One key, many models", roles: ["embedding", "assistant", "semantic"],
        configured: false, locked: [], docs_url: "https://openrouter.ai/docs", cost_note: "Pay per model", privacy_note: "Routes to third-party providers",
        values: {},
        fields: [{ key: "OPENROUTER_API_KEY", label: "API key", kind: "secret", required: true, placeholder: "sk-or-…", help: null, help_url: "https://openrouter.ai/keys", options: [], default: null }],
        models: {
          embedding: [{ id: "openai/text-embedding-3-large", label: "text-embedding-3-large", default: true }],
          assistant: [{ id: "anthropic/claude-3.5-sonnet", label: "Claude 3.5 Sonnet", default: true }, { id: "openai/gpt-4o", label: "GPT-4o" }],
          semantic: [{ id: "anthropic/claude-3.5-haiku", label: "Claude 3.5 Haiku", default: true, note: "cheap" }, { id: "openai/gpt-4o-mini", label: "GPT-4o mini" }],
        },
      },
      {
        id: "openai", name: "OpenAI", tagline: "GPT & embeddings", roles: ["embedding", "assistant", "semantic"],
        configured: false, locked: [], docs_url: "https://platform.openai.com/docs", cost_note: "Pay as you go", privacy_note: "Text is sent to OpenAI",
        values: {},
        fields: [{ key: "OPENAI_API_KEY", label: "API key", kind: "secret", required: true, placeholder: "sk-…", help: null, help_url: null, options: [], default: null }],
        models: {
          embedding: [{ id: "text-embedding-3-small", label: "text-embedding-3-small", default: true }],
          assistant: [{ id: "gpt-4o", label: "GPT-4o", default: true }, { id: "gpt-4o-mini", label: "GPT-4o mini" }],
          semantic: [{ id: "gpt-4o-mini", label: "GPT-4o mini", default: true }],
        },
      },
      {
        id: "gemini", name: "Gemini", tagline: "Google's models", roles: ["embedding", "assistant", "semantic"],
        configured: false, locked: [], docs_url: "https://ai.google.dev", cost_note: "Generous free tier", privacy_note: "Text is sent to Google",
        values: {},
        fields: [{ key: "GEMINI_API_KEY", label: "API key", kind: "secret", required: true, placeholder: "AIza…", help: null, help_url: "https://aistudio.google.com/apikey", options: [], default: null }],
        models: {
          embedding: [{ id: "text-embedding-004", label: "text-embedding-004", default: true }],
          assistant: [{ id: "gemini-1.5-pro", label: "Gemini 1.5 Pro", default: true }],
          semantic: [{ id: "gemini-1.5-flash", label: "Gemini 1.5 Flash", default: true, note: "fast & cheap" }],
        },
      },
      {
        id: "anthropic", name: "Anthropic", tagline: "Claude models", roles: ["assistant"],
        configured: false, locked: [], docs_url: "https://docs.anthropic.com", cost_note: "Pay as you go", privacy_note: "Text is sent to Anthropic",
        values: {},
        fields: [{ key: "ANTHROPIC_API_KEY", label: "API key", kind: "secret", required: true, placeholder: "sk-ant-…", help: null, help_url: null, options: [], default: null }],
        models: { assistant: [{ id: "claude-3-5-sonnet-latest", label: "Claude 3.5 Sonnet", default: true }, { id: "claude-3-5-haiku-latest", label: "Claude 3.5 Haiku", note: "faster" }] },
      },
      {
        id: "cli_proxy", name: "CLI proxy", tagline: "Any OpenAI-compatible base URL", roles: ["assistant", "semantic"],
        configured: false, locked: [], docs_url: null, cost_note: "Free with your own proxy", privacy_note: "Stays on your network",
        values: {},
        fields: [
          { key: "CLI_PROXY_BASE_URL", label: "Base URL", kind: "url", required: true, placeholder: "http://localhost:8080/v1", help: null, help_url: null, options: [], default: null },
          { key: "CLI_PROXY_API_KEY", label: "API key", kind: "secret", required: false, placeholder: "optional", help: null, help_url: null, options: [], default: null },
        ],
        models: {},   // no fixed list — models are discovered via Test (detail.models)
      },
      {
        id: "local", name: "Local server", tagline: "An OpenAI-compatible server you run", roles: ["embedding", "assistant", "semantic"],
        configured: false, locked: ["LOCAL_BASE_URL"], docs_url: null, cost_note: "Free", privacy_note: "Nothing leaves your machine",
        values: { LOCAL_BASE_URL: "http://127.0.0.1:11434/v1" },
        fields: [{ key: "LOCAL_BASE_URL", label: "Base URL", kind: "url", required: true, placeholder: "http://127.0.0.1:11434/v1", help: "Set by the server environment", help_url: null, options: [], default: null }],
        models: {},   // discovered via Test
      },
      {
        id: "immich", name: "Immich", tagline: "Your self-hosted photos", roles: ["images"],
        configured: false, locked: [], docs_url: "https://immich.app", cost_note: null, privacy_note: "Your photos stay on your server",
        values: {},
        fields: [
          { key: "IMMICH_URL", label: "Immich URL", kind: "url", required: true, placeholder: "https://photos.example.com", help: "Your Immich instance", help_url: null, options: [], default: null },
          { key: "IMMICH_API_KEY", label: "API key", kind: "secret", required: true, placeholder: "…", help: "Account Settings → API Keys", help_url: null, options: [], default: null },
        ],
        models: {},
      },
    ],
  };

  /* ---- catalog ---- */
  const categories = [
    { id: "messaging", label: "Messaging" },
    { id: "mail", label: "Mail" },
    { id: "calendar", label: "Calendar & meetings" },
    { id: "location", label: "Location" },
    { id: "media", label: "Media" },
    { id: "health", label: "Health" },
    { id: "dev", label: "Developer" },
    { id: "migration", label: "Migrations" },
    { id: "notes", label: "Notes" },
  ];

  const field = (key: string, label: string, kind: string, required: boolean, extra: Partial<Json> = {}): Json => ({
    key, label, kind, required, help: "", help_url: null, placeholder: null, default: null, options: [], advanced: false, when: {}, ...extra,
  });

  const mkStatus = (over: Partial<Json> = {}): Json => ({
    installed: false, installable: true, install_hint: null, configured: false, enabled: false, connected: false,
    records: 0, last_at: null, last_error: null, values: {}, missing: [], ...over,
  });

  const mkPlugin = (p: Json): Json => ({
    repo_id: "starter", homepage: null, tags: [], channels: [], platforms: ["macos", "windows", "linux"],
    requires: [], pairs_with: [], starter: false, check: true,
    backfill: { supported: true, history: "full", note: "" },
    schedule: { interval_s: 3600, mode: "poll" },
    setup: { steps: [], fields: [] },
    status: mkStatus(),
    ...p,
  });

  const plugins: Json[] = [
    mkPlugin({
      id: "wave", name: "Wave", summary: "Your Wave messages and threads", description: "Import every conversation from Wave, with full history and attachments.",
      category: "messaging", icon: "message-square", starter: true,
      setup: { steps: [{ title: "Open Wave settings", body: "Find Developer → Tokens", url: "https://wave.example.com/settings" }, { title: "Create a token", body: "Scope it to read messages", url: null }], fields: [field("WAVE_TOKEN", "API token", "secret", true, { help: "A read token from Wave" }), field("WAVE_WORKSPACE", "Workspace", "text", false)] },
      status: mkStatus({ installed: true, configured: true }),
    }),
    mkPlugin({
      id: "granola", name: "Granola", summary: "Meeting notes and transcripts", description: "Bring in your Granola meeting transcripts and summaries.",
      category: "calendar", icon: "calendar", starter: true, backfill: { supported: true, history: "window", note: "Recent meetings" },
      setup: { steps: [{ title: "Copy your API key", body: "From Granola → Settings → API", url: null }], fields: [field("GRANOLA_API_KEY", "API key", "secret", true)] },
      status: mkStatus({ installed: true, configured: true }),
    }),
    mkPlugin({
      id: "gmail", name: "Gmail / IMAP", summary: "Your mail, over IMAP", description: "Sync mail from Gmail or any IMAP server. Uses an app password, never your real one.",
      category: "mail", icon: "mail", starter: true, requires: ["network"],
      setup: { steps: [{ title: "Turn on IMAP", body: "Gmail → Settings → Forwarding and POP/IMAP", url: "https://mail.google.com/mail/u/0/#settings/fwdandpop" }, { title: "Create an app password", body: "Google Account → Security → App passwords", url: "https://myaccount.google.com/apppasswords" }], fields: [field("IMAP_EMAIL", "Email address", "email", true), field("IMAP_PASSWORD", "App password", "secret", true), field("IMAP_HOST", "IMAP host", "text", false, { default: "imap.gmail.com", advanced: true }), field("IMAP_PORT", "Port", "number", false, { default: "993", advanced: true })] },
      status: mkStatus({ installed: true, configured: true }),
    }),
    mkPlugin({
      id: "google-timeline", name: "Google Timeline", summary: "Your location history", description: "Import your full location history from a Google Takeout / Timeline export. Shown as points on the map and timeline.",
      category: "location", icon: "map-pin", starter: true, requires: ["upload"],
      setup: { steps: [{ title: "Export your Timeline", body: "takeout.google.com → Location History (Timeline)", url: "https://takeout.google.com" }, { title: "Upload the JSON", body: "Drop the Records.json file below", url: null }], fields: [field("TIMELINE_EXPORT", "Timeline export", "file", true, { placeholder: "Records.json" })] },
      status: mkStatus({ installed: true, configured: true }),
    }),
    mkPlugin({
      id: "beeper", name: "Beeper", summary: "All your chat networks, bridged", description: "Beeper bridges Discord, WhatsApp, Signal and more. Runs through the companion app on your computer.",
      category: "messaging", icon: "messages-square", starter: true, requires: ["companion"], pairs_with: ["discord"],
      setup: { steps: [{ title: "Sign in to Beeper", body: "In the companion app", url: null }], fields: [field("BEEPER_TOKEN", "Access token", "secret", false)] },
      status: mkStatus({ installed: false, installable: false, install_hint: "Beeper runs through the desktop companion. Install and pair it in the Companion step." }),
    }),
    mkPlugin({
      id: "discord", name: "Discord", summary: "Your Discord DMs and servers (via Beeper)", description: "Discord messages arrive through the Beeper bridge.",
      category: "messaging", icon: "message-circle", pairs_with: ["beeper"], requires: ["companion"],
      setup: { steps: [{ title: "Connect Discord in Beeper", body: "Beeper → Chat networks → Discord", url: null }], fields: [] },
      status: mkStatus({ installed: false, installable: false, install_hint: "Enable Discord inside Beeper, then it syncs through the companion." }),
    }),
    mkPlugin({
      id: "gbrain", name: "GBrain", summary: "Migrate from a GBrain install", description: "Bring your archive over from an existing GBrain database.",
      category: "migration", icon: "brain", backfill: { supported: true, history: "full", note: "Everything" },
      setup: { steps: [{ title: "Find your GBrain database URL", body: "From the GBrain config", url: null }], fields: [field("GBRAIN_DB_URL", "Database URL", "url", true, { placeholder: "postgres://…" })] },
      status: mkStatus({ installed: true, configured: false, missing: ["GBRAIN_DB_URL"] }),
    }),
    mkPlugin({
      id: "claude-code", name: "Claude Code", summary: "Your coding-agent sessions", description: "Index your Claude Code transcripts from this machine. Reads local logs through the companion app.",
      category: "dev", icon: "terminal", requires: ["companion"], backfill: { supported: true, history: "window", note: "Recent sessions" },
      setup: { steps: [{ title: "Install the companion", body: "It reads your local ~/.claude logs", url: null }], fields: [] },
      status: mkStatus({ installed: false, installable: false, install_hint: "Claude Code logs live on your computer, so this runs through the companion app." }),
    }),
    mkPlugin({
      id: "spotify", name: "Spotify", summary: "What you've listened to", description: "Your Spotify listening history and saved tracks.",
      category: "media", icon: "music", setup: { steps: [{ title: "Create an app", body: "developer.spotify.com → Dashboard", url: "https://developer.spotify.com/dashboard" }], fields: [field("SPOTIFY_CLIENT_ID", "Client ID", "text", true), field("SPOTIFY_CLIENT_SECRET", "Client secret", "secret", true)] },
    }),
    mkPlugin({
      id: "oura", name: "Oura", summary: "Sleep, readiness and activity", description: "Your Oura ring health metrics.",
      category: "health", icon: "heart-pulse", setup: { steps: [{ title: "Create a personal token", body: "cloud.ouraring.com → Personal Access Tokens", url: "https://cloud.ouraring.com/personal-access-tokens" }], fields: [field("OURA_TOKEN", "Personal token", "secret", true)] },
      status: mkStatus({ installed: true, configured: true, enabled: true, connected: true, records: 3650, last_at: now() }),
    }),
    mkPlugin({
      id: "notion", name: "Notion", summary: "Your notes and docs", description: "Import pages and databases from your Notion workspace.",
      category: "notes", icon: "notebook", setup: { steps: [{ title: "Create an integration", body: "notion.so/my-integrations", url: "https://www.notion.so/my-integrations" }], fields: [field("NOTION_TOKEN", "Integration token", "secret", true)] },
    }),
    mkPlugin({
      id: "slack", name: "Slack", summary: "Your Slack messages", description: "Import messages from the Slack workspaces you're in.",
      category: "messaging", icon: "hash", setup: { steps: [{ title: "Create a token", body: "api.slack.com/apps", url: "https://api.slack.com/apps" }], fields: [field("SLACK_TOKEN", "Token", "secret", true)] },
    }),
  ];

  const repos: Json[] = [
    { id: "starter", name: "Bundled connectors", url: null, description: "The connectors that ship with Throughline", builtin: true, commit: null, added_at: null, plugins: plugins.map((p) => p.id as string) },
  ];
  const catalogProblems: Json[] = [];

  /* ---- jobs (time-driven) ---- */
  interface JobSeed { id: number; kind: string; plugin_id: string | null; title: string; status: string; startedAt: number; duration: number; target: number; unit: string; oldest: string | null; newest: string | null; fail?: boolean; phaseText?: string; }
  const jobSeeds: JobSeed[] = [
    { id: 1, kind: "backfill", plugin_id: "gmail", title: "Importing Gmail", status: "running", startedAt: t0, duration: 50, target: 42_000, unit: "records", oldest: "2014-03-01T00:00:00Z", newest: now() },
    { id: 2, kind: "backfill", plugin_id: "google-timeline", title: "Importing Google Timeline", status: "running", startedAt: t0, duration: 70, target: 337_000, unit: "points", oldest: "2015-04-01T00:00:00Z", newest: now() },
    { id: 3, kind: "backfill", plugin_id: "wave", title: "Importing Wave", status: "failed", startedAt: t0, duration: 20, target: 54_000, unit: "records", oldest: "2016-06-01T00:00:00Z", newest: now(), fail: true },
  ];
  let nextJobId = 100;

  const renderJob = (s: JobSeed): Json => {
    const elapsed = (Date.now() - s.startedAt) / 1000;
    const done = s.status === "running" && elapsed >= s.duration;
    const frac = s.fail ? 0.18 : Math.min(elapsed / s.duration, 1);
    const status = s.fail ? "failed" : done ? "done" : s.status;
    const delivered = s.unit === "records" ? Math.round(frac * s.target) : 0;
    const tracks = s.unit === "points" ? Math.round(frac * s.target) : 0;
    return {
      id: s.id, kind: s.kind, plugin_id: s.plugin_id, title: s.title, status,
      phase: status === "failed" ? "Stopped" : status === "done" ? "Done" : s.phaseText ?? (s.unit === "points" ? `Reading ${new Date(s.newest ?? "").getFullYear()}` : "Reading mailboxes"),
      progress: {
        delivered, tracks, oldest: s.oldest, newest: s.newest,
        done: null, total: null, unit: s.unit,
        fraction: s.unit === "points" && status === "running" ? null : frac,
        rate_per_s: status === "running" ? Math.round(s.target / s.duration) : null,
        eta_s: status === "running" ? Math.max(0, (1 - frac) * s.duration) : null,
      },
      error: s.fail ? { title: "Wave rejected the token", message: "Wave returned 401 Unauthorized while reading messages.", hint: "Create a fresh read token in Wave settings and save it again.", code: "unauthorized", problems: [{ where: "WAVE_TOKEN", message: "token expired or revoked" }] } : null,
      log_tail: s.fail ? ["GET /api/messages?page=3 → 200", "GET /api/messages?page=4 → 401", "auth: token rejected (expired)"] : [],
      started_at: new Date(s.startedAt).toISOString(), finished_at: status === "done" || status === "failed" ? now() : null, created_at: new Date(s.startedAt).toISOString(),
    };
  };

  const jobsPayload = (): Json => {
    const rendered = jobSeeds.map(renderJob);
    const running = rendered.filter((j) => j.status === "running");
    const fracs = running.map((j) => (j.progress as Json).fraction as number | null).filter((f): f is number => f != null);
    return {
      jobs: rendered,
      summary: {
        running: running.length,
        queued: 0,
        failed: rendered.filter((j) => j.status === "failed").length,
        done: rendered.filter((j) => j.status === "done").length,
        fraction: fracs.length ? fracs.reduce((a, b) => a + b, 0) / fracs.length : null,
        delivered: rendered.reduce((n, j) => n + ((j.progress as Json).delivered as number), 0),
      },
    };
  };

  /* ---- people ---- */
  const personCard = (id: number, name: string, handles: Array<[string, string, string]>, messages: number, last: string): Json => ({
    id, name, handles: handles.map(([system, kind, value]) => ({ system, kind, value })), messages, last_at: last,
  });
  const people: Json = {
    me: null,
    me_candidates: [
      { ...(personCard(1, "You (Wave)", [["wave", "handle", "@you"]], 12000, now()) as object), reason: "Most messages sent from this account" },
      { ...(personCard(2, "you@gmail.com", [["gmail", "email", "you@gmail.com"]], 8000, now()) as object), reason: "Your primary mail address" },
    ],
    stats: { people: 418, identifiers: 1240, unclaimed: 36, auto_merged: 212, created: 418, questions_open: 3, answered: 0 },
    auto: [
      { name: "Robin Vale", merged: ["robin@work.example.com", "@robin (Wave)", "Robin P. (Granola)"], reason: "same display name and overlapping threads", confidence: 0.97 },
      { name: "Marcus Lee", merged: ["marcus.lee@gmail.com", "@mlee (Discord)"], reason: "shared phone number", confidence: 0.95 },
    ],
    questions: [
      { id: 11, prompt: "Is “Birdie” on Discord the same person as Robin Vale?", confidence: 0.62, reasons: ["They message the same group thread", "Similar active hours", "No conflicting real name"], a: personCard(31, "Birdie", [["discord", "handle", "birdie#4821"]], 540, now()), b: personCard(32, "Robin Vale", [["gmail", "email", "robin@work.example.com"], ["wave", "handle", "@robin"]], 2300, now()), suggested_name: "Robin Vale", status: "open" },
      { id: 12, prompt: "Are these two the same person?", confidence: 0.4, reasons: ["Same first name", "Different time zones"], a: personCard(33, "Sam (Slack)", [["slack", "handle", "@sam"]], 120, now()), b: personCard(34, "Samuel Ortiz", [["gmail", "email", "sam.ortiz@x.com"]], 60, now()), suggested_name: "Samuel Ortiz", status: "open" },
      { id: 13, prompt: "Is this work contact also your personal contact?", confidence: 0.81, reasons: ["Same phone number across both", "Overlapping calendar invites"], a: personCard(35, "J. Rivera (work)", [["gmail", "email", "j.rivera@corp.com"]], 900, now()), b: personCard(36, "Jess", [["wave", "handle", "@jess"]], 1500, now()), suggested_name: "Jess Rivera", status: "open" },
    ],
    running: null,
    llm_assist: true,
  };

  /* ---- companion ---- */
  const companion: Json = {
    devices: [],
    downloads: [
      { os: "macos", label: "Download for macOS", url: "https://throughline.example/companion/mac", note: "Apple silicon & Intel" },
      { os: "windows", label: "Download for Windows", url: "https://throughline.example/companion/win" },
      { os: "linux", label: "Download for Linux", url: "https://throughline.example/companion/linux" },
    ],
    install_command: "uv tool install throughline-companion",
    owner_ready: true,
  };
  let pairing: Json | null = null;

  /* ---- agent ---- */
  const agent: Json = {
    mcp_url: "https://hub.local/v1/mcp",
    api_url: "https://hub.local/v1",
    skill_url: "https://hub.local/v1/skill",
    tokens: [],
    clients: [
      { id: "claude-code", label: "Claude Code", kind: "mcp", steps: ["Run the command below in your project", "Restart Claude Code"], snippet: "claude mcp add throughline --transport http https://hub.local/v1/mcp --header 'Authorization: Bearer TOKEN'" },
      { id: "claude-desktop", label: "Claude Desktop", kind: "mcp", steps: ["Open Settings → Developer → Edit config", "Add the server block below"], snippet: '{\n  "mcpServers": {\n    "throughline": {\n      "url": "https://hub.local/v1/mcp",\n      "headers": { "Authorization": "Bearer TOKEN" }\n    }\n  }\n}' },
      { id: "cursor", label: "Cursor", kind: "mcp", steps: ["Settings → MCP → Add server"], snippet: '{ "throughline": { "url": "https://hub.local/v1/mcp", "headers": { "Authorization": "Bearer TOKEN" } } }' },
      { id: "codex", label: "Codex", kind: "mcp", steps: ["Add to ~/.codex/config.toml"], snippet: "[mcp.throughline]\nurl = \"https://hub.local/v1/mcp\"\nbearer = \"TOKEN\"" },
      { id: "chatgpt", label: "ChatGPT", kind: "http", steps: ["Add a custom connector pointing at the URL below"], snippet: "https://hub.local/v1/mcp" },
      { id: "other", label: "Other", kind: "http", steps: ["Point any MCP client at the URL with a bearer token"], snippet: "https://hub.local/v1/mcp" },
    ],
  };

  const okCheck = (title: string, message: string): Json => ({ ok: true, title, message, hint: null, duration_ms: 120 + Math.round(Math.random() * 180) });

  /* ---- paces (shared by lenses + pages) ---- */
  const paces: Json[] = [
    { id: "live", label: "Live", description: "Updates within minutes of a new message. Most calls, highest cost.", lens_schedule: "*/5 * * * *", page_schedule: "*/15 * * * *", lookback_days: 7, max_cost_per_run: 0.2, calls_per_day: 288 },
    { id: "balanced", label: "Balanced", description: "Catches up a few times a day. A sensible default for most people.", lens_schedule: "0 */6 * * *", page_schedule: "0 */12 * * *", lookback_days: 30, max_cost_per_run: 0.05, calls_per_day: 4 },
    { id: "thrifty", label: "Thrifty", description: "Once a day, newest first. The lowest cost, slightly behind.", lens_schedule: "0 3 * * *", page_schedule: "0 4 * * *", lookback_days: 90, max_cost_per_run: 0.01, calls_per_day: 1 },
  ];
  const semanticModel: Json = { ready: true, provider: "cli_proxy", model: "llama-3.1-70b-instruct", hint: null };
  const twelveDaysAgo = new Date(Date.now() - 12 * 86_400_000).toISOString();

  /* ---- lenses ---- */
  const lensTemplates: Json[] = [
    { id: "plans", title: "Plans", pitch: "Upcoming things you've agreed to — trips, deadlines, appointments.", icon: "calendar-check", lens: "starter_plans", columns: ["what", "when", "with_whom", "status"], available: true, why_not: null, enabled: false, rows: 0 },
    { id: "recommendations", title: "Recommendations", pitch: "Books, films, places and tools people suggested to you.", icon: "thumbs-up", lens: "starter_recs", columns: ["item", "kind", "recommended_by"], available: true, why_not: null, enabled: false, rows: 0 },
    { id: "important_dates", title: "Important dates", pitch: "Birthdays, anniversaries and dates that matter to people you know.", icon: "cake", lens: "starter_dates", columns: ["person", "date", "occasion"], available: true, why_not: null, enabled: false, rows: 0 },
  ];
  const lensesView: Json = {
    model: semanticModel,
    paces,
    pace: "balanced",
    channels: ["wave.dm", "gmail.inbox", "granola.meetings"],
    templates: lensTemplates,
    others: [{ name: "reading_list", rows: 42, description: "Reading list — things you've said you want to read" }],
  };
  const lensRows: Record<string, Array<Json>> = {
    starter_plans: [
      { id: 1, what: "Trip to Lisbon with Robin", when: "2026-12-14", with_whom: "Robin Vale", status: "planned" },
      { id: 2, what: "Send the Q4 report", when: "2026-11-28", with_whom: "work", status: "due" },
      { id: 3, what: "Dentist appointment", when: "2026-12-02", with_whom: null, status: "confirmed" },
    ],
    starter_recs: [
      { id: 1, item: "Piranesi", kind: "book", recommended_by: "Emily" },
      { id: 2, item: "Bar Baratti", kind: "place", recommended_by: "Marcus" },
    ],
    starter_dates: [
      { id: 1, person: "Robin Vale", date: "03-21", occasion: "birthday" },
      { id: 2, person: "Mum", date: "09-02", occasion: "birthday" },
    ],
  };

  /* ---- pages ---- */
  const pageFamilies: Json[] = [
    { id: "daily", lens: "daily_pages", kind: "days", title: "Daily", pitch: "A page for each day — what happened, who you spoke to, what's still open.", icon: "calendar-days", enabled: false, available: true, why_not: null, pages: 0 },
    { id: "people", lens: "people_pages", kind: "people", title: "People", pitch: "A page per person — your whole history with them at a glance.", icon: "users", enabled: false, available: true, why_not: null, pages: 0 },
    { id: "events", lens: "event_pages", kind: "events", title: "Events", pitch: "A page for every gathering, trip or event.", icon: "calendar-range", enabled: false, available: true, why_not: null, pages: 0 },
  ];
  const pagesView: Json = {
    available: true, why_not: null, families: pageFamilies, paces, pace: "balanced",
    model: semanticModel, markdown_dir: null, sinks_config: "/data/pages.toml",
  };
  const dailyContent = `# Tuesday, 18 November 2026\n\nA quiet, productive day. Most of it went to the Q4 report; you promised Marcus a draft by Friday.\n\n## People\n\n- **Robin Vale** — confirmed the Lisbon dates (14–19 Dec).\n- **Emily Pease** — recommended *Piranesi*.\n\n## Open loops\n\n- [ ] Send the Q4 report draft to Marcus\n- [ ] Book the Lisbon flights\n\n> "I'll get you the draft by Friday" — to Marcus, 14:22\n`;
  const pageSubjects: Record<string, Array<Json>> = {
    daily: [
      { subject_key: "2026-11-18", title: "Tuesday, 18 November 2026", page_message_id: 101, status: "current", period_start: "2026-11-18", period_end: "2026-11-18", last_built_at: now() },
      { subject_key: "2026-11-17", title: "Monday, 17 November 2026", page_message_id: 101, status: "current", period_start: "2026-11-17", period_end: "2026-11-17", last_built_at: now() },
      { subject_key: "2026-11-16", title: "Sunday, 16 November 2026", page_message_id: 101, status: "current", period_start: "2026-11-16", period_end: "2026-11-16", last_built_at: now() },
    ],
    people: [{ subject_key: "robin-vale", title: "Robin Vale", page_message_id: 101, status: "current", period_start: null, period_end: null, last_built_at: now() }],
    events: [{ subject_key: "2026-11-18-lisbon-planning", title: "Lisbon planning call", page_message_id: 101, status: "current", period_start: "2026-11-18", period_end: "2026-11-18", last_built_at: now() }],
  };
  const kindFamily: Record<string, string> = { days: "daily", people: "people", events: "events" };
  const pageDoc = (kind: string, key: string): Json => ({
    title: (pageSubjects[kindFamily[kind] ?? kind]?.find((s) => (s as Json).subject_key === key) as Json | undefined)?.title ?? key,
    slug: `${kind}/${key}`, kind, body: dailyContent, summary: "A quiet, productive day.",
    updated_at: now(), version: 1, message_id: 1, citations: [],
  });

  /* ---- starter queries ---- */
  const starterQueries: Json = {
    tz: "UTC",
    queries: [
      { id: "promises", title: "Promises this week", question: "What did I say I'd do this week?", explain: "Searches your messages by meaning for commitments you made.", kind: "find", request: { query: "I'll send OR I promise OR I'll get you OR I will", k: 6 } },
      { id: "busiest", title: "Your busiest channels", question: "Where do most of my messages live?", explain: "Counts records per channel across your whole archive.", kind: "sql", request: { statement: "SELECT channel, count(*) AS messages\nFROM api.messages\nGROUP BY channel\nORDER BY messages DESC\nLIMIT 10" } },
      { id: "berlin", title: "When did “Berlin” come up?", question: "Every time Berlin was mentioned, across the years.", explain: "An exhaustive keyword timeline, grouped by episode.", kind: "timeline", request: { query: "Berlin" } },
    ],
  };
  const sqlResponse: Json = {
    truncated: false, row_count: 5,
    rows: [
      { channel: "wave.dm", messages: 54211 },
      { channel: "gmail.inbox", messages: 42088 },
      { channel: "discord.dm", messages: 18233 },
      { channel: "granola.meetings", messages: 1204 },
      { channel: "slack.general", messages: 980 },
    ],
  };
  const findResponse: Json = {
    query: "promises",
    episodes: [
      { rank: 1, channel: "wave.dm", thread: "Marcus Lee", time_from: "2026-11-14T14:22:00Z", time_to: "2026-11-14T14:25:00Z", messages: [{ time: "2026-11-14T14:22:00Z", author: "you (owner)", text: "I'll get you the Q4 draft by Friday, promise." }] },
      { rank: 2, channel: "gmail.inbox", thread: "Lisbon trip", time_from: "2026-11-10T09:00:00Z", time_to: "2026-11-10T09:02:00Z", messages: [{ time: "2026-11-10T09:00:00Z", author: "you (owner)", text: "I'll book the flights this weekend and send the itinerary." }] },
    ],
    note: "Ranked sample, not an exhaustive list.", searched: { k: 6 },
  };
  const timelineResponse: Json = {
    query: "Berlin", total_matches: 37, total_episodes: 21, keyword_episodes: 21, semantic_only_episodes: 0,
    episodes_by_year: { "2019": 2, "2021": 5, "2022": 6, "2023": 4, "2024": 3, "2026": 1 },
    episodes_by_channel: { "wave.dm": 12, "gmail.inbox": 9 },
    episodes: [
      { time: "2022-06-02T18:00:00Z", channel: "wave.dm", thread: "Trip planning", first: { text: "Should we do Berlin in September?" }, matches: 3, ids: [] },
      { time: "2021-03-11T10:00:00Z", channel: "gmail.inbox", thread: "Conference", first: { text: "The Berlin conference is confirmed for May." }, matches: 2, ids: [] },
    ],
    exact: true, note: "Exhaustive keyword matches grouped into episodes.",
  };

  /* ---- bootstrap / auth ---- */
  const bootstrap: Json = {
    version: "mock", now: now(),
    token: { id: "mock", kind: "owner", name: "web", asker: null, unrestricted: true, can_write: true },
    features: { web_state: false, source_health: true, embeddings: { available: true, model: "voyage-3" }, assistant: { provider: null, model: null, loopback: true, default_tier: "schema" }, egress: {} },
    owner_person_id: null,
    totals: { records: detection.totals && (detection.totals as Json).records as number, channels: 11, plugins: 12, people: 418, entities: 0, links: 0, threads: 0, vectors: 0 },
    plugins: [], channels: [], degraded: false,
  };

  const account = { id: 1, handle: "you", display_name: "You", email: null };
  const session = { method: "password", persistent: true, created_at: now(), expires_at: new Date(Date.now() + 86_400_000).toISOString() };

  const json = (route: Route, body: unknown, status = 200): Promise<void> =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

  page.route("**/v1/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace(/^\/v1/, "");
    const method = req.method();
    const body = (): Json => { try { return JSON.parse(req.postData() ?? "{}") as Json; } catch { return {}; } };

    // auth
    if (path === "/auth/setup" && method === "GET") return json(route, { needs_account: Boolean(opts.needsAccount), code_required: true, code_hint: "Printed by `throughline serve` on startup", instance_name: "your hub" });
    if (path === "/auth/setup" && method === "POST") { opts.needsAccount = false; return json(route, { account, session }); }
    if (path === "/auth/session") return json(route, opts.needsAccount ? { account: null } : { account, session });
    if (path === "/auth/config") return json(route, { methods: { password: true, token: true }, providers: [], accounts: true });
    if (path === "/auth/logout") return json(route, { ok: true });

    // bootstrap / status
    if (path === "/web/bootstrap") return json(route, bootstrap);
    if (path === "/status") return json(route, { embeddings: { available: true, models: [{ model: "voyage-3" }] }, plugins: [], channels: [], staleness: {} });

    // setup state
    if (path === "/setup/state" && method === "GET") return json(route, state);
    if (path === "/setup/state" && method === "PUT") {
      const b = body();
      if (b.steps) state.steps = { ...(state.steps as Json), ...(b.steps as Json) };
      if (b.choices) state.choices = { ...(state.choices as Json), ...(b.choices as Json) };
      if (b.mode) state.mode = b.mode;
      state.status = "in_progress";
      return json(route, state);
    }
    if (path === "/setup/complete") { state.status = "done"; state.completed_at = now(); return json(route, state); }
    if (path === "/setup/reset") { state.status = "fresh"; state.steps = {}; return json(route, state); }
    if (path === "/setup/detect") return json(route, detection);

    // providers
    if (path === "/setup/providers" && method === "GET") return json(route, providers);
    if (path.match(/^\/setup\/providers\/[^/]+$/) && method === "PUT") {
      const id = decodeURIComponent(path.split("/")[3]!);
      const prov = (providers.providers as Json[]).find((p) => p.id === id);
      if (prov) prov.configured = true;
      return json(route, prov ?? {});
    }
    if (path.match(/^\/setup\/providers\/[^/]+\/test$/)) {
      const id = decodeURIComponent(path.split("/")[3]!);
      if (id === "local") return json(route, { ok: false, title: "Couldn't reach the local server", message: "Connection refused at http://127.0.0.1:11434/v1.", hint: "Make sure your local model server is running, then test again.", duration_ms: 90 }, 200);
      // cli_proxy / local report their available models via detail.models.
      if (id === "cli_proxy") return json(route, { ok: true, title: "Connection good", message: "Your proxy answered with 3 models.", hint: null, duration_ms: 140, detail: { models: ["llama-3.1-70b-instruct", "qwen2.5-coder-32b", "mixtral-8x7b"] } });
      return json(route, okCheck("Connection good", `${id} responded and the key is valid.`));
    }
    if (path === "/setup/roles" && method === "PUT") {
      const b = body();
      providers.roles = { ...(providers.roles as Json), ...b };
      return json(route, providers);
    }

    // catalog
    if (path === "/setup/catalog") return json(route, { categories, repos, plugins, problems: catalogProblems });
    if (path === "/setup/repos" && method === "POST") {
      const b = body();
      const u = String(b.url ?? "");
      if (!/github\.com/.test(u)) return json(route, { detail: { title: "That doesn't look like a repository", message: "The URL must point at a Git repository.", hint: "Paste the repo's https URL, e.g. https://github.com/you/throughline-plugins", problems: [{ where: "url", message: "not a recognised git URL" }] } }, 400);
      const repoId = "community";
      if (!repos.find((r) => r.id === repoId)) {
        repos.push({ id: repoId, name: "Community connectors", url: u, description: "Added from GitHub", builtin: false, commit: "a1b2c3d", added_at: now(), plugins: ["obsidian"] });
        plugins.push(mkPlugin({ id: "obsidian", repo_id: repoId, name: "Obsidian", summary: "Your Obsidian vault notes", description: "Index markdown notes from an Obsidian vault folder.", category: "notes", icon: "notebook-pen", setup: { steps: [{ title: "Point at your vault", body: "The folder that holds your .md files", url: null }], fields: [field("VAULT_PATH", "Vault path", "directory", true, { placeholder: "/home/you/vault" })] } }));
      }
      return json(route, repos.find((r) => r.id === repoId));
    }
    if (path.match(/^\/setup\/repos\/[^/]+$/) && method === "DELETE") {
      const id = decodeURIComponent(path.split("/")[3]!);
      const idx = repos.findIndex((r) => r.id === id);
      if (idx >= 0 && !repos[idx]!.builtin) { repos.splice(idx, 1); for (let i = plugins.length - 1; i >= 0; i--) if (plugins[i]!.repo_id === id) plugins.splice(i, 1); }
      return json(route, { ok: true });
    }
    if (path.match(/^\/setup\/plugins\/[^/]+\/install$/)) {
      const id = decodeURIComponent(path.split("/")[3]!);
      const plug = plugins.find((p) => p.id === id);
      if (plug) (plug.status as Json).installed = true;
      const jid = nextJobId++;
      jobSeeds.push({ id: jid, kind: "install", plugin_id: id, title: `Installing ${id}`, status: "running", startedAt: Date.now(), duration: 4, target: 1, unit: "records", oldest: null, newest: null });
      return json(route, renderJob(jobSeeds.find((j) => j.id === jid)!));
    }
    if (path.match(/^\/setup\/plugins\/[^/]+\/config$/)) {
      const id = decodeURIComponent(path.split("/")[3]!);
      const b = body();
      const plug = plugins.find((p) => p.id === id);
      if (plug) { const st = plug.status as Json; st.configured = true; if (b.enabled !== undefined) st.enabled = Boolean(b.enabled); st.last_error = null; }
      return json(route, plug ?? {});
    }
    if (path.match(/^\/setup\/plugins\/[^/]+\/check$/)) {
      const id = decodeURIComponent(path.split("/")[3]!);
      if (id === "gbrain") return json(route, { ok: false, title: "Couldn't reach the GBrain database", message: "Connection to the database URL was refused.", hint: "Check the host is reachable from this server and the credentials are right.", detail: null, duration_ms: 210 });
      return json(route, okCheck("Looks good", `${id} is reachable and configured correctly.`));
    }
    if (path.match(/^\/setup\/plugins\/[^/]+\/backfill$/)) {
      const id = decodeURIComponent(path.split("/")[3]!);
      const existing = jobSeeds.find((j) => j.plugin_id === id && j.kind === "backfill");
      if (existing) { existing.status = "running"; existing.startedAt = Date.now(); existing.fail = false; return json(route, renderJob(existing)); }
      const jid = nextJobId++;
      const seed: JobSeed = { id: jid, kind: "backfill", plugin_id: id, title: `Importing ${id}`, status: "running", startedAt: Date.now(), duration: 45, target: 20_000, unit: "records", oldest: "2018-01-01T00:00:00Z", newest: now() };
      jobSeeds.push(seed);
      return json(route, renderJob(seed));
    }
    if (path === "/setup/uploads") return json(route, { path: "/data/uploads/Records.json", name: "Records.json", size: 10_485_760 });

    // jobs
    if (path === "/setup/jobs" && method === "GET") return json(route, jobsPayload());
    if (path.match(/^\/setup\/jobs\/\d+\/cancel$/)) { const jid = Number(path.split("/")[3]); const s = jobSeeds.find((j) => j.id === jid); if (s) s.status = "cancelled"; return json(route, s ? renderJob(s) : {}); }
    if (path.match(/^\/setup\/jobs\/\d+\/retry$/)) { const jid = Number(path.split("/")[3]); const s = jobSeeds.find((j) => j.id === jid); if (s) { s.status = "running"; s.startedAt = Date.now(); s.fail = false; } return json(route, s ? renderJob(s) : {}); }
    if (path.match(/^\/setup\/jobs\/\d+$/)) { const jid = Number(path.split("/")[3]); const s = jobSeeds.find((j) => j.id === jid); return json(route, s ? renderJob(s) : {}, s ? 200 : 404); }

    // people
    if (path === "/setup/people" && method === "GET") return json(route, people);
    if (path === "/setup/people/resolve") { (people.stats as Json).questions_open = (people.questions as Json[]).filter((q) => q.status === "open").length; return json(route, { id: 90, kind: "people", plugin_id: null, title: "Linking people", status: "done", phase: "Done", progress: { delivered: 0, tracks: 0, oldest: null, newest: null, done: null, total: null, unit: null, fraction: 1, rate_per_s: null, eta_s: null }, error: null, log_tail: [], started_at: now(), finished_at: now(), created_at: now() }); }
    if (path === "/setup/people/me") { const b = body(); people.me = personCard(Number(b.person_id ?? 1), String(b.display_name ?? "You"), [["wave", "handle", "@you"]], 12000, now()); return json(route, people); }
    if (path.match(/^\/setup\/people\/questions\/\d+$/)) {
      const qid = Number(path.split("/")[4]);
      const b = body();
      const q = (people.questions as Json[]).find((x) => x.id === qid);
      if (q) q.status = b.answer === "same" ? "same" : b.answer === "different" ? "different" : "skipped";
      const st = people.stats as Json;
      st.answered = (st.answered as number) + 1;
      st.questions_open = (people.questions as Json[]).filter((x) => x.status === "open").length;
      return json(route, people);
    }

    // lenses
    if (path === "/setup/lenses" && method === "GET") return json(route, lensesView);
    if (path === "/setup/lenses" && method === "PUT") {
      const b = body();
      const templates = Array.isArray(b.templates) ? (b.templates as string[]) : [];
      if (b.pace) lensesView.pace = b.pace;
      const quotes: Record<string, Json> = {
        plans: { items: 412, calls: 9, tokens: 120_000, cost_usd: 0.04, since: twelveDaysAgo, within_monthly_cap: true },
        recommendations: { items: 88, calls: 2, cost_usd: 0, since: twelveDaysAgo, within_monthly_cap: true },
        important_dates: { error: "Needs the people resolver to run first" },
      };
      const lensesApplied: Json[] = [];
      for (const t of lensTemplates) {
        if (!templates.includes(t.id as string)) continue;
        if (!(quotes[t.id as string] as Json)?.error) t.enabled = true;
        lensesApplied.push({ name: t.lens, template: t.id, quote: quotes[t.id as string] ?? { cost_usd: 0 } });
      }
      return json(route, { lenses: lensesApplied, view: lensesView });
    }
    if (path.match(/^\/setup\/lenses\/[^/]+\/run$/)) {
      const name = decodeURIComponent(path.split("/")[3]!);
      const jid = nextJobId++;
      const seed: JobSeed = { id: jid, kind: "lens", plugin_id: null, title: `Filling ${name}`, status: "running", startedAt: Date.now(), duration: 3, target: 120, unit: "records", oldest: null, newest: null, phaseText: "Reading your conversations" };
      jobSeeds.push(seed);
      return json(route, renderJob(seed));
    }
    if (path.match(/^\/lenses\/[^/]+\/rows$/)) {
      const name = decodeURIComponent(path.split("/")[2]!);
      return json(route, { lens: name, view: `api.lens_${name}`, rows: lensRows[name] ?? [], row_count: (lensRows[name] ?? []).length });
    }

    // pages
    if (path === "/setup/pages" && method === "GET") return json(route, pagesView);
    if (path === "/setup/pages" && method === "PUT") {
      const b = body();
      const families = Array.isArray(b.families) ? (b.families as string[]) : [];
      if (b.pace) pagesView.pace = b.pace;
      pagesView.markdown_dir = b.markdown_dir ?? null;
      for (const f of pageFamilies) if (families.includes(f.id as string)) f.enabled = true;
      const plans: Json = {};
      if (families.includes("daily")) plans.daily = { due: 3 };
      if (families.includes("people")) plans.people = { due: 12 };
      if (families.includes("events")) plans.events = { due: 1 };
      return json(route, { written: [], plans, view: pagesView });
    }
    if (path.match(/^\/setup\/pages\/[^/]+\/run$/)) {
      const family = decodeURIComponent(path.split("/")[3]!);
      const jid = nextJobId++;
      const seed: JobSeed = { id: jid, kind: "pages", plugin_id: null, title: `Writing ${family} pages`, status: "running", startedAt: Date.now(), duration: 3, target: 3, unit: "records", oldest: null, newest: null, phaseText: `Writing ${family} pages` };
      jobSeeds.push(seed);
      const fam = pageFamilies.find((f) => f.id === family);
      if (fam) fam.pages = (pageSubjects[family] ?? []).length;
      return json(route, renderJob(seed));
    }
    if (path === "/pages" && method === "GET") {
      const kind = url.searchParams.get("kind") ?? "days";
      const subjects = pageSubjects[kindFamily[kind] ?? kind] ?? [];
      return json(route, subjects.map((x) => ({ slug: `${kind}/${(x as Json).subject_key}`, kind, title: (x as Json).title, summary: null, updated_at: now() })));
    }
    if (path.match(/^\/pages\/[^/]+\/[^/]+$/)) {
      const parts = path.split("/");
      return json(route, pageDoc(decodeURIComponent(parts[2]!), decodeURIComponent(parts[3]!)));
    }

    // queries
    if (path === "/setup/queries") return json(route, starterQueries);
    if (path === "/sql" && method === "POST") return json(route, sqlResponse);
    if (path === "/find" && method === "POST") return json(route, findResponse);
    if (path === "/timeline" && method === "POST") return json(route, timelineResponse);

    // companion
    if (path === "/setup/companion" && method === "GET") return json(route, companion);
    if (path === "/setup/companion/pair") { pairing = { name: "This computer", code: "tlc1.KQ8F-2M7X-9ZB4", plugins: ["beeper", "claude-code"], expires_at: new Date(Date.now() + 600_000).toISOString(), command: "throughline-companion pair tlc1.KQ8F-2M7X-9ZB4" }; return json(route, pairing); }

    // agent
    if (path === "/setup/agent" && method === "GET") return json(route, agent);
    if (path === "/setup/agent/token") { const b = body(); const token = "tl_owner_8f3kQ2zR9wXv"; const clients = (agent.clients as Json[]).map((c) => ({ ...c, snippet: String(c.snippet).replace(/TOKEN/g, token) })); return json(route, { id: 1, name: String(b.name ?? "agent"), token, clients }); }
    if (path === "/setup/prompt") return json(route, { markdown: "# Set up Throughline\n\nYou are helping configure a Throughline hub. Ask the user which sources they use, then:\n\n1. Call `GET /v1/setup/catalog` to list connectors.\n2. For each chosen source, save its config and run its check.\n3. Start backfills and report progress.\n\nBe concise and confirm each step." });

    return json(route, { detail: `not mocked: ${method} ${path}` }, 404);
  });
}

/** Seed a fake token + theme before the app loads, so the app is "signed in". */
export async function seedAuth(page: Page, theme: "nocturne" | "daybook" = "nocturne"): Promise<void> {
  await page.addInitScript(
    ([th]) => {
      try {
        sessionStorage.setItem("tl.token", "mock-token");
        localStorage.setItem("tl.prefs", JSON.stringify({ theme: th, density: "observatory", veil: false, mapTiles: false, disclosure: "schema", tz: "UTC" }));
        sessionStorage.removeItem("tl.setup.deferred");
        sessionStorage.removeItem("tl.setup.banner.dismissed");
      } catch {
        /* ignore */
      }
    },
    [theme] as const,
  );
}
