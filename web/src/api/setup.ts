/*
 * Onboarding / setup API (src/throughline/api/routes/setup.py). Owner only,
 * except the pre-sign-in pair under /v1/auth/setup.
 *
 * Errors: every failure is an HTTP error whose `detail` is a SetupProblem
 * (title + human sentence + what to do next, and optionally a list of
 * {where, message} problems), so screens can show *why* and *what next*
 * instead of a status code. Use `setupProblem(err)` to read one.
 *
 *  Pre-sign-in (public)
 *   GET  /v1/auth/setup                 -> PublicSetup
 *   POST /v1/auth/setup  {code, handle, display_name?, password}
 *                                       -> {account, session}   (creates the first owner account, signs in)
 *
 *  State
 *   GET  /v1/setup/state                -> SetupState
 *   PUT  /v1/setup/state  {steps?, choices?}   -> SetupState  (merge)
 *   POST /v1/setup/complete             -> SetupState
 *   POST /v1/setup/reset                -> SetupState  (forget progress, keeps data)
 *   GET  /v1/setup/detect               -> Detection
 *
 *  Model providers & credentials
 *   GET  /v1/setup/providers            -> ProvidersView
 *   PUT  /v1/setup/providers/{id}  {values}          -> ProviderStatus
 *   POST /v1/setup/providers/{id}/test {values?}     -> CheckResult
 *   PUT  /v1/setup/roles  {embedding?, assistant?, semantic?}  -> ProvidersView
 *
 *  Plugins
 *   GET    /v1/setup/catalog             -> Catalog
 *   POST   /v1/setup/repos {url}         -> PluginRepo        (clone + validate a GitHub repo)
 *   DELETE /v1/setup/repos/{id}          -> {ok}
 *   POST   /v1/setup/plugins/{id}/install          -> Job
 *   PUT    /v1/setup/plugins/{id}/config {values, enabled?} -> CatalogPlugin
 *   POST   /v1/setup/plugins/{id}/check  {values?}  -> CheckResult
 *   POST   /v1/setup/plugins/{id}/backfill          -> Job
 *   POST   /v1/setup/uploads?plugin=&field=  (raw body, header X-Filename) -> Upload
 *
 *  Jobs (installs, backfills, people resolution)
 *   GET  /v1/setup/jobs                  -> {jobs: Job[], summary: JobSummary}
 *   GET  /v1/setup/jobs/{id}             -> Job
 *   POST /v1/setup/jobs/{id}/cancel      -> Job
 *   POST /v1/setup/jobs/{id}/retry       -> Job
 *
 *  People
 *   GET  /v1/setup/people                -> PeopleView
 *   POST /v1/setup/people/resolve        -> Job      (the resolver run; questions appear as it goes)
 *   POST /v1/setup/people/me  {identifier_ids?, display_name?} -> PeopleView
 *   POST /v1/setup/people/questions/{id}  {answer, name?}      -> PeopleView
 *
 *  First lenses, pages and queries
 *   GET  /v1/setup/lenses                -> LensesView
 *   PUT  /v1/setup/lenses {templates, pace} -> {lenses: LensApplied[], view: LensesView}
 *   POST /v1/setup/lenses/{name}/run     -> Job (kind "lens"; progress.delivered = rows)
 *   GET  /v1/lenses/{name}/rows          -> existing route: the lens's rows
 *   GET  /v1/setup/pages                 -> PagesView
 *   PUT  /v1/setup/pages {families, pace, markdown_dir?} -> {written, plans, view: PagesView}
 *   POST /v1/setup/pages/{family}/run    -> Job (kind "pages"; progress.delivered = pages written)
 *   GET  /v1/pages/{family}              -> existing route: the family and its pages
 *   GET  /v1/pages/{family}/{key}        -> existing route: one page (markdown content)
 *   GET  /v1/setup/queries               -> {queries: StarterQuery[], tz}
 *     run each through the existing POST /v1/sql | /v1/find | /v1/timeline
 *
 *  Companion
 *   POST /v1/setup/companion/pair {name} -> CompanionPairing
 *   GET  /v1/setup/companion             -> CompanionView
 *
 *  Agents
 *   GET  /v1/setup/agent                 -> AgentView
 *   POST /v1/setup/agent/token {name, client} -> AgentToken (token shown once)
 *   GET  /v1/setup/prompt                -> {markdown}  (the copyable setup prompt for an agent)
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { request } from "./client";
import { ApiError } from "./errors";

/* ───────────── errors ───────────── */

export interface SetupProblem {
  title: string;
  message: string;
  hint?: string | null;
  code?: string | null;
  problems?: Array<{ where: string; message: string }>;
  docs_url?: string | null;
}

export function setupProblem(err: unknown): SetupProblem {
  if (err instanceof ApiError) {
    const d = err.detail as Partial<SetupProblem> | undefined;
    if (d && typeof d === "object" && typeof d.title === "string") {
      return { title: d.title, message: d.message ?? err.message, hint: d.hint, code: d.code, problems: d.problems, docs_url: d.docs_url };
    }
    if (err.status === 0) return { title: "Can't reach Throughline", message: "The browser could not reach the server.", hint: "Check that `throughline serve` is still running." };
    return { title: `Request failed (HTTP ${err.status})`, message: err.message };
  }
  return { title: "Something went wrong", message: err instanceof Error ? err.message : String(err) };
}

/* ───────────── state ───────────── */

export type StepId =
  | "welcome" | "account" | "models" | "photos" | "sources" | "connect"
  | "companion" | "import" | "people" | "lenses" | "pages" | "queries" | "agent" | "tour";

export type StepStatus = "todo" | "done" | "skipped";

export interface PublicSetup {
  needs_account: boolean;       // no account exists yet
  code_required: boolean;       // creating it needs the setup code printed by `throughline serve`
  code_hint: string;            // where to find the code
  instance_name: string;
  providers?: Array<{ id: string; kind: string; label: string; start: string }>;
  redirect_uri?: string;
  can_configure_oidc?: boolean;
  owner_ready?: boolean;
}

export interface SetupState {
  status: "fresh" | "in_progress" | "done";
  mode: "fresh" | "restore" | "migrate";
  steps: Partial<Record<StepId, { status: StepStatus; at?: string | null }>>;
  choices: {
    sources?: string[];          // selected plugin ids
    migrate_from?: string[];     // e.g. ["gbrain", "hermes"]
    [k: string]: unknown;
  };
  owner_ready: boolean;          // server can mint tokens / pair devices (owner DB connection available)
  owner_hint?: string | null;    // why not, and what to run instead
  started_at?: string | null;
  completed_at?: string | null;
}

export interface DetectedSource {
  plugin_id: string;             // catalog id
  name: string;
  connected: boolean;            // data already present (restore / migration / external runner)
  managed: boolean;              // installed & scheduled by Throughline itself
  records: number;
  first_at: string | null;
  last_at: string | null;
  health: "ok" | "failing" | "stalled" | "down" | "never" | "paused" | null;
}

export interface Detection {
  existing: boolean;             // install already holds data → suggest "restore / continue"
  totals: { records: number; people: number; channels: number; plugins: number; track_points: number };
  sources: DetectedSource[];
  providers: { embedding: string | null; assistant: string | null };
  companion_devices: number;
  accounts: number;
  restored_hint: boolean;        // THROUGHLINE_SETUP_MODE=restore or a restore marker
}

export type RestoreScope = "database" | "config" | "lenses" | "pages";

export interface BackupBundleSummary {
  name: string;
  dir: string;
  started_at: string | null;
  finished_at: string | null;
  ok: boolean;
  encryption: string | null;
  bytes: number;
  available_scopes: RestoreScope[];
  parts: Array<{ name: string | null; kind: string | null; file: string | null; bytes: number | null; present: boolean }>;
}

export interface RestoreView {
  backup_dir: string;
  passphrase_present: boolean;
  encryption_kind: string;
  owner_ready: boolean;
  owner_hint?: string | null;
  scopes: RestoreScope[];
  bundles: BackupBundleSummary[];
  detected: Detection;
}

export interface RestoreResult {
  ok: boolean;
  bundle: string | null;
  scopes: RestoreScope[];
  restored: Record<string, unknown>;
  skipped: string[];
  onboarding_open: boolean;
  state: SetupState;
  detected: Detection;
}

/* ───────────── providers ───────────── */

export type ProviderRole = "embedding" | "assistant" | "semantic" | "images";

export interface ProviderField {
  key: string;                   // env var name
  label: string;
  kind: "secret" | "text" | "url" | "select";
  required: boolean;
  placeholder?: string | null;
  help?: string | null;
  help_url?: string | null;
  options?: string[];
  default?: string | null;
}

export interface ProviderStatus {
  id: string;                    // openrouter | voyage | openai | anthropic | gemini | cli_proxy | local | immich
  name: string;
  tagline: string;
  roles: ProviderRole[];
  fields: ProviderField[];
  configured: boolean;
  values: Record<string, string | null>;   // non-secret values; secrets as "••••last4" or null
  locked: string[];              // keys set in the server's environment (not editable here)
  docs_url?: string | null;
  cost_note?: string | null;
  privacy_note?: string | null;
  models: Partial<Record<ProviderRole, Array<{ id: string; label: string; note?: string; default?: boolean }>>>;
}

export interface RoleChoice { provider: string | null; model?: string | null }

export interface ProvidersView {
  providers: ProviderStatus[];
  roles: Record<"embedding" | "assistant" | "semantic", RoleChoice>;
  egress_allow: string[];
}

export interface CheckResult {
  ok: boolean;
  title: string;
  message: string;
  hint?: string | null;
  detail?: Record<string, unknown> | null;
  duration_ms?: number;
}

/* ───────────── plugins ───────────── */

export type FieldKind =
  | "text" | "secret" | "url" | "email" | "path" | "directory" | "file"
  | "select" | "boolean" | "number" | "list" | "textarea";

export interface SetupField {
  key: string;
  label: string;
  kind: FieldKind;
  required: boolean;
  help: string;
  help_url: string | null;
  placeholder: string | null;
  default: string | null;
  options: string[];
  advanced: boolean;
  when: Record<string, string[]>;
}

export interface CatalogPlugin {
  id: string;
  repo_id: string;
  name: string;
  summary: string;
  description: string;
  category: string;
  icon: string;                  // lucide icon name
  homepage: string | null;
  tags: string[];
  channels: string[];
  platforms: string[];
  requires: Array<"companion" | "docker" | "upload" | "network">;
  pairs_with: string[];
  starter: boolean;
  backfill: { supported: boolean; history: "full" | "window" | "none"; note: string };
  schedule: { interval_s: number; mode: string };
  setup: { steps: Array<{ title: string; body: string; url: string | null }>; fields: SetupField[] };
  check: boolean;
  status: {
    installed: boolean;
    installable: boolean;        // false → `install_hint` explains (e.g. needs the companion)
    install_hint: string | null;
    configured: boolean;         // every required field has a value
    enabled: boolean;            // scheduled for recurring sync
    connected: boolean;          // data present
    records: number;
    last_at: string | null;
    last_error: string | null;
    values: Record<string, string | null>;   // secrets masked
    missing: string[];           // required keys without a value
  };
}

export interface PluginRepo {
  id: string;
  name: string;
  url: string | null;            // null for the bundled starter set
  description: string;
  builtin: boolean;
  commit: string | null;
  added_at: string | null;
  plugins: string[];             // plugin ids
}

export interface Catalog {
  categories: Array<{ id: string; label: string }>;
  repos: PluginRepo[];
  plugins: CatalogPlugin[];
  /** Non-fatal repo issues (e.g. a repo that cloned but failed to validate), shown non-blockingly. */
  problems?: Array<{ repo_id: string; message: string }>;
}

export interface Upload { path: string; name: string; size: number }

/* ───────────── jobs ───────────── */

export type JobStatus = "queued" | "running" | "done" | "failed" | "cancelled" | "interrupted";

export interface Job {
  id: number;
  kind: "install" | "backfill" | "sync" | "people" | "check" | "lens" | "pages";
  plugin_id: string | null;
  title: string;
  status: JobStatus;
  phase: string | null;          // human phrase: "Reading 2023", "Linking people"
  progress: {
    delivered: number;           // records written
    tracks: number;              // location points written
    oldest: string | null;
    newest: string | null;
    done: number | null;
    total: number | null;
    unit: string | null;
    fraction: number | null;     // 0..1 when known
    rate_per_s: number | null;
    eta_s: number | null;
  };
  error: SetupProblem | null;
  log_tail: string[];
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface JobSummary {
  running: number;
  queued: number;
  failed: number;
  done: number;
  fraction: number | null;       // overall, when computable
  delivered: number;
}

/* ───────────── people ───────────── */

export interface PersonCard {
  id: number;
  name: string;
  handles: Array<{ system: string; kind: string; value: string }>;
  messages: number;
  last_at: string | null;
}

export interface MergeQuestion {
  id: number;
  prompt: string;                // natural sentence: "Is Birdie on Discord the same person as Robin Vale?"
  confidence: number;            // 0..1
  reasons: string[];             // evidence bullets
  a: PersonCard;
  b: PersonCard;
  suggested_name: string | null;
  status: "open" | "same" | "different" | "skipped" | "auto";
}

export interface PeopleView {
  me: PersonCard | null;
  me_candidates: Array<PersonCard & { reason: string; identifier_ids?: number[] }>;
  stats: {
    people: number;
    identifiers: number;
    unclaimed: number;
    auto_merged: number;
    created: number;
    questions_open: number;
    answered: number;
  };
  auto: Array<{ name: string; merged: string[]; reason: string; confidence: number }>;
  questions: MergeQuestion[];
  running: Job | null;
  llm_assist: boolean;           // an assistant model is configured and was used
}

/* ───────────── lenses, pages, queries ───────────── */

export type PaceId = "live" | "balanced" | "thrifty";

export interface Pace {
  id: PaceId;
  label: string;
  description: string;
  lens_schedule: string;
  page_schedule: string;
  lookback_days: number;
  max_cost_per_run: number;
  calls_per_day: number;      // rough runs per day, for a "speed vs cost" readout
}

export interface ModelStatus { ready: boolean; provider: string | null; model: string | null; hint: string | null }

export interface LensTemplate {
  id: string;                 // plans | recommendations | important_dates
  title: string;
  pitch: string;
  icon: string;               // lucide name
  lens: string;               // lens name, e.g. starter_plans (SQL view api.lens_<lens>)
  columns: string[];
  available: boolean;
  why_not: string | null;
  enabled: boolean;
  rows: number;
}

export interface LensesView {
  model: ModelStatus;
  paces: Pace[];
  pace: PaceId;
  channels: string[];         // conversation channels the lenses will read
  templates: LensTemplate[];
  others: Array<{ name: string; rows: number; description: string }>;
}

export interface LensApplied {
  name: string;
  template: string;
  quote: { items?: number; calls?: number; tokens?: number; cost_usd?: number; since?: string; within_monthly_cap?: boolean; error?: string };
}

export interface PageFamily {
  id: "daily" | "people" | "events";
  lens: string;               // the pages lens behind it (output = "pages")
  kind: string;               // the page kind it writes (days, people, events)
  title: string;
  pitch: string;
  icon: string;
  enabled: boolean;
  available: boolean;
  why_not: string | null;
  pages: number;              // pages written so far
}

export interface PagesView {
  available: boolean;
  why_not: string | null;
  families: PageFamily[];
  paces: Pace[];
  pace: PaceId;
  model: ModelStatus;
  markdown_dir: string | null;
  sinks_config?: string;
}

export interface StarterQuery {
  id: string;
  title: string;
  question: string;
  explain: string;
  kind: "sql" | "find" | "timeline";
  request: Record<string, unknown>;   // body for POST /v1/sql | /v1/find | /v1/timeline
}

/* ───────────── companion & agents ───────────── */

export interface CompanionView {
  devices: Array<{ name: string; last_seen_at: string | null; plugins: string[]; platform: string | null }>;
  downloads: Array<{ os: "macos" | "windows" | "linux"; label: string; url: string; note?: string }>;
  install_command: string;       // e.g. `uv tool install throughline-companion`
  owner_ready: boolean;
}

export interface CompanionPairing {
  name: string;
  code: string;                  // tlc1.… (paste into the app)
  plugins: string[];
  expires_at: string | null;
  command: string;               // `throughline-companion pair <code>`
}

export interface AgentClient {
  id: "claude-code" | "claude-desktop" | "cursor" | "codex" | "chatgpt" | "other";
  label: string;
  kind: "mcp" | "skill" | "http";
  snippet: string;               // with a TOKEN placeholder until a token is minted
  steps: string[];
}

export interface AgentView {
  mcp_url: string;
  api_url: string;
  skill_url: string;
  clients: AgentClient[];
  tokens: Array<{ id: number; name: string; created_at: string; last_used_at: string | null }>;
}

export interface AgentToken { id: number; name: string; token: string; clients: AgentClient[] }

/* ───────────── calls ───────────── */

const STATE = ["setup", "state"] as const;
const DETECT = ["setup", "detect"] as const;
const PROVIDERS = ["setup", "providers"] as const;
const CATALOG = ["setup", "catalog"] as const;
const JOBS = ["setup", "jobs"] as const;
const PEOPLE = ["setup", "people"] as const;

export const getPublicSetup = () => request<PublicSetup>("/auth/setup", { noAuthRedirect: true });
export const createFirstAccount = (body: { code: string; handle: string; display_name?: string; email?: string; password: string }) =>
  request<{ account: unknown; session: unknown }>("/auth/setup", { method: "POST", body, noAuthRedirect: true });
export const startFirstAccountOidc = (body: {
  code: string;
  provider_id?: string;
  kind?: "authentik" | "oidc";
  label?: string;
  issuer?: string;
  client_id?: string;
  client_secret?: string;
  allowed_groups?: string;
  link_by_email?: boolean;
  handle?: string;
  remember?: boolean;
}) =>
  request<{ authorize_url: string; provider: { id: string; kind: string; label: string; start: string } }>(
    "/auth/setup/oidc",
    { method: "POST", body, noAuthRedirect: true },
  );

export interface AuthProviderConfig {
  id: string;
  kind: string;
  label: string;
  issuer: string;
  client_id: string;
  client_secret_set: boolean;
  client_secret_hint: string | null;
  scopes: string;
  allowed_groups: string[];
  link_by_email: boolean;
  redirect_uri: string | null;
  callback_url: string;
  locked: boolean;
}

export interface AccountIdentityLink {
  provider: string;
  subject: string;
  issuer: string | null;
  email: string | null;
  username: string | null;
  linked_at: string | null;
  last_login_at: string | null;
}

export interface SetupAuthView {
  public_url: string;
  public_url_locked: boolean;
  default_redirect_uri: string;
  password_enabled: boolean;
  password_locked: boolean;
  token_enabled: boolean;
  token_locked: boolean;
  providers_locked: boolean;
  can_configure_oidc: boolean;
  providers: AuthProviderConfig[];
  problems: string[];
  current_account: {
    id: number;
    handle: string;
    display_name: string | null;
    email: string | null;
    has_password: boolean;
    links: AccountIdentityLink[];
  } | null;
  owner_ready: boolean;
  owner_hint: string | null;
}

const AUTH_SETUP = ["setup", "auth"] as const;

export function useSetupAuth() {
  return useQuery({
    queryKey: AUTH_SETUP,
    queryFn: ({ signal }) => request<SetupAuthView>("/setup/auth", { signal }),
    staleTime: 15_000,
  });
}

export function useSaveAuthProvider() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      id?: string;
      kind?: "authentik" | "oidc";
      label?: string;
      issuer: string;
      client_id: string;
      client_secret?: string;
      scopes?: string;
      allowed_groups?: string;
      link_by_email?: boolean;
      redirect_uri?: string;
      public_url?: string;
      verify?: boolean;
    }) => request<SetupAuthView>("/setup/auth/provider", { method: "PUT", body }),
    onSuccess: (v) => qc.setQueryData(AUTH_SETUP, v),
  });
}

export function useRemoveAuthProvider() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (providerId: string) =>
      request<SetupAuthView>(`/setup/auth/provider/${encodeURIComponent(providerId)}`, { method: "DELETE" }),
    onSuccess: (v) => qc.setQueryData(AUTH_SETUP, v),
  });
}

export function useUpdateAuthMethods() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { password?: boolean; token?: boolean; public_url?: string }) =>
      request<SetupAuthView>("/setup/auth/methods", { method: "PUT", body }),
    onSuccess: (v) => qc.setQueryData(AUTH_SETUP, v),
  });
}

export function useStartAuthLink() {
  return useMutation({
    mutationFn: ({ providerId, returnTo }: { providerId: string; returnTo?: string }) =>
      request<{ authorize_url: string; provider: { id: string; kind: string; label: string } }>(
        `/setup/auth/oidc/${encodeURIComponent(providerId)}/link`,
        { method: "POST", body: returnTo ? { return_to: returnTo } : {} },
      ),
  });
}

export function useSetupState() {
  return useQuery({ queryKey: STATE, queryFn: ({ signal }) => request<SetupState>("/setup/state", { signal }), staleTime: 10_000 });
}
export function useSaveSetupState() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: Partial<Pick<SetupState, "steps" | "choices" | "mode">>) => request<SetupState>("/setup/state", { method: "PUT", body: patch }),
    onSuccess: (s) => qc.setQueryData(STATE, s),
  });
}
export function useCompleteSetup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => request<SetupState>("/setup/complete", { method: "POST", body: {} }),
    onSuccess: (s) => { qc.setQueryData(STATE, s); void qc.invalidateQueries({ queryKey: ["bootstrap"] }); },
  });
}
export function useDetection() {
  return useQuery({ queryKey: DETECT, queryFn: ({ signal }) => request<Detection>("/setup/detect", { signal }), staleTime: 30_000 });
}
export function useRestoreView(enabled = true) {
  return useQuery({
    queryKey: ["setup", "restore"] as const,
    queryFn: ({ signal }) => request<RestoreView>("/setup/restore", { signal }),
    enabled,
    staleTime: 15_000,
  });
}
export function useApplyRestore() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { bundle?: string | null; parts?: RestoreScope[]; overwrite?: boolean; clean?: boolean; continue_onboarding?: boolean }) =>
      request<RestoreResult>("/setup/restore", { method: "POST", body }),
    onSuccess: (r) => {
      qc.setQueryData(STATE, r.state);
      qc.setQueryData(DETECT, r.detected);
      void qc.invalidateQueries({ queryKey: PROVIDERS });
      void qc.invalidateQueries({ queryKey: CATALOG });
      void qc.invalidateQueries({ queryKey: ["setup", "restore"] });
    },
  });
}

export function useProviders() {
  return useQuery({ queryKey: PROVIDERS, queryFn: ({ signal }) => request<ProvidersView>("/setup/providers", { signal }) });
}
export function useSaveProvider() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, values }: { id: string; values: Record<string, string> }) =>
      request<ProviderStatus>(`/setup/providers/${encodeURIComponent(id)}`, { method: "PUT", body: { values } }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: PROVIDERS }),
  });
}
export const testProvider = (id: string, values?: Record<string, string>) =>
  request<CheckResult>(`/setup/providers/${encodeURIComponent(id)}/test`, { method: "POST", body: { values: values ?? null } });
export function useSaveRoles() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (roles: Partial<Record<"embedding" | "assistant" | "semantic", RoleChoice>>) =>
      request<ProvidersView>("/setup/roles", { method: "PUT", body: roles }),
    onSuccess: (v) => qc.setQueryData(PROVIDERS, v),
  });
}

export function useCatalog() {
  return useQuery({ queryKey: CATALOG, queryFn: ({ signal }) => request<Catalog>("/setup/catalog", { signal }), staleTime: 15_000 });
}
export function useAddRepo() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (url: string) => request<PluginRepo>("/setup/repos", { method: "POST", body: { url } }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: CATALOG }),
  });
}
export function useRemoveRepo() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => request<{ ok: boolean }>(`/setup/repos/${encodeURIComponent(id)}`, { method: "DELETE" }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: CATALOG }),
  });
}
export function useSavePluginConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, values, enabled }: { id: string; values: Record<string, string>; enabled?: boolean }) =>
      request<CatalogPlugin>(`/setup/plugins/${encodeURIComponent(id)}/config`, { method: "PUT", body: { values, enabled } }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: CATALOG }),
  });
}
export const checkPlugin = (id: string, values?: Record<string, string>) =>
  request<CheckResult>(`/setup/plugins/${encodeURIComponent(id)}/check`, { method: "POST", body: { values: values ?? null } });
export function useInstallPlugin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => request<Job>(`/setup/plugins/${encodeURIComponent(id)}/install`, { method: "POST", body: {} }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: JOBS }); void qc.invalidateQueries({ queryKey: CATALOG }); },
  });
}
export function useStartBackfill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => request<Job>(`/setup/plugins/${encodeURIComponent(id)}/backfill`, { method: "POST", body: {} }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: JOBS }),
  });
}

/** Upload an export file (e.g. Google Timeline's Timeline.json). Raw body, so large files stream. */
export async function uploadExport(plugin: string, field: string, file: File, onProgress?: (fraction: number) => void): Promise<Upload> {
  const { auth } = await import("@/store/auth");
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/v1/setup/uploads?plugin=${encodeURIComponent(plugin)}&field=${encodeURIComponent(field)}`);
    const token = auth.token();
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.setRequestHeader("X-Throughline-Client", "web");
    xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.withCredentials = true;
    xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let body: unknown = null;
      try { body = JSON.parse(xhr.responseText); } catch { /* not json */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as Upload);
      else reject(new ApiError({ status: xhr.status, message: (body as { detail?: { message?: string } })?.detail?.message ?? "upload failed", detail: (body as { detail?: unknown })?.detail }));
    };
    xhr.onerror = () => reject(new ApiError({ status: 0, message: "could not reach the server" }));
    xhr.send(file);
  });
}

export function useJobs(poll = true) {
  return useQuery({
    queryKey: JOBS,
    queryFn: ({ signal }) => request<{ jobs: Job[]; summary: JobSummary }>("/setup/jobs", { signal }),
    refetchInterval: poll ? 1000 : false,
    staleTime: 0,
  });
}
export const cancelJob = (id: number) => request<Job>(`/setup/jobs/${id}/cancel`, { method: "POST", body: {} });
export const retryJob = (id: number) => request<Job>(`/setup/jobs/${id}/retry`, { method: "POST", body: {} });

export function usePeople(poll = false) {
  return useQuery({
    queryKey: PEOPLE,
    queryFn: ({ signal }) => request<PeopleView>("/setup/people", { signal }),
    refetchInterval: poll ? 1500 : false,
  });
}
export function useResolvePeople() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => request<Job>("/setup/people/resolve", { method: "POST", body: {} }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: PEOPLE }),
  });
}
export function useSetMe() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { person_id?: number; identifier_ids?: number[]; display_name?: string }) =>
      request<PeopleView>("/setup/people/me", { method: "POST", body }),
    onSuccess: (v) => qc.setQueryData(PEOPLE, v),
  });
}
export function useAnswerQuestion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, answer, name }: { id: number; answer: "same" | "different" | "skip"; name?: string }) =>
      request<PeopleView>(`/setup/people/questions/${id}`, { method: "POST", body: { answer, name } }),
    onSuccess: (v) => qc.setQueryData(PEOPLE, v),
  });
}

export function useLensesSetup() {
  return useQuery({ queryKey: ["setup", "lenses"], queryFn: ({ signal }) => request<LensesView>("/setup/lenses", { signal }) });
}
export function useApplyLenses() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { templates: string[]; pace: PaceId }) =>
      request<{ lenses: LensApplied[]; view: LensesView }>("/setup/lenses", { method: "PUT", body }),
    onSuccess: (r) => { qc.setQueryData(["setup", "lenses"], r.view); void qc.invalidateQueries({ queryKey: STATE }); },
  });
}
export const runLens = (name: string) => request<Job>(`/setup/lenses/${encodeURIComponent(name)}/run`, { method: "POST", body: {} });
export function usePagesSetup() {
  return useQuery({ queryKey: ["setup", "pages"], queryFn: ({ signal }) => request<PagesView>("/setup/pages", { signal }) });
}
export function useApplyPages() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { families: string[]; pace: PaceId; markdown_dir?: string | null }) =>
      request<{ written: string[]; plans: Record<string, { due?: number; error?: string }>; view: PagesView }>("/setup/pages", { method: "PUT", body }),
    onSuccess: (r) => { qc.setQueryData(["setup", "pages"], r.view); void qc.invalidateQueries({ queryKey: STATE }); },
  });
}
export const runPages = (family: string) => request<Job>(`/setup/pages/${encodeURIComponent(family)}/run`, { method: "POST", body: {} });
export function useStarterQueries() {
  return useQuery({ queryKey: ["setup", "queries"], queryFn: ({ signal }) => request<{ queries: StarterQuery[]; tz: string }>("/setup/queries", { signal }) });
}

export function useCompanion() {
  return useQuery({ queryKey: ["setup", "companion"], queryFn: ({ signal }) => request<CompanionView>("/setup/companion", { signal }), refetchInterval: 3000 });
}
export const pairCompanion = (name: string) => request<CompanionPairing>("/setup/companion/pair", { method: "POST", body: { name } });

export function useAgentView() {
  return useQuery({ queryKey: ["setup", "agent"], queryFn: ({ signal }) => request<AgentView>("/setup/agent", { signal }) });
}
export const mintAgentToken = (name: string, client: string) =>
  request<AgentToken>("/setup/agent/token", { method: "POST", body: { name, client } });
export const getSetupPrompt = () => request<{ markdown: string }>("/setup/prompt");
