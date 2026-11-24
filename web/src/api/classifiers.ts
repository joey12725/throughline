/*
 * Classifiers: owner-defined jobs that write annotations on messages.
 *
 *   GET  /v1/classifiers                       -> { classifiers, sandbox }
 *   GET  /v1/classifiers/guide                 -> ClassifierGuide
 *   GET  /v1/classifiers/{name}                -> ClassifierDetail
 *   GET  /v1/classifiers/{name}/results        -> PythonResults | MessageResults
 *   POST /v1/classifiers                       define/update   -> { classifier, check, status, receipt }
 *   POST /v1/classifiers/check                 validate only   -> CheckResult
 *   POST /v1/classifiers/try                   spends model calls -> TryResult
 *   POST /v1/classifiers/draft                 LLM drafts a definition
 *   POST /v1/classifiers/{name}/status|run|backfill|retract-marks
 *   GET  /v1/annotation-attributes             -> { attributes }
 *   GET  /v1/threads/{id}/classification       -> ThreadClassification
 *   POST /v1/group-annotations                 owner correction
 *   POST /v1/group-annotations/{id}/retract    undo it
 *
 * Every write carries an Idempotency-Key so a double click cannot double-run.
 */
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { request } from "./client";

export type ClassifierKind = "message" | "python";
export type ClassifierStatus = "proposed" | "active" | "paused" | "rejected";
export type RunStatus = "running" | "done" | "partial" | "failed";

export interface RunError {
  error: string;
  thread_id?: string | number;
  message_id?: string | number;
}

export interface ClassifierRun {
  id: number | string;
  trigger: string;
  status: RunStatus;
  started_at: string;
  finished_at: string | null;
  items: number;
  marks: number;
  calls: number;
  tokens: number;
  cost: number;
  errors: RunError[];
  summary: unknown;
}

export interface ClassifierStats {
  annotations: number;
  group_annotations: number;
  threads_judged: number;
  month_cost: number;
  total_cost: number;
  pending_messages?: number;
  pending_threads?: number;
  last_run: ClassifierRun | null;
}

export interface Classifier {
  id: number | string;
  name: string;
  kind: ClassifierKind;
  description: string | null;
  attribute: string;
  channels: string[];
  spec: Record<string, unknown>;
  version: number;
  status: ClassifierStatus;
  monthly_cap_usd: number | null;
  has_code: boolean;
  last_run_at: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
  stats: ClassifierStats;
}

export interface ValueFieldSpec {
  type: string;
  required?: boolean;
  values?: Array<string | number>;
  min?: number;
  max?: number;
  description?: string;
}
export type ValueSchema = Record<string, ValueFieldSpec>;

export interface AnnotationAttribute {
  name: string;
  description: string | null;
  value_schema: ValueSchema;
  recall_effect: "none" | "exclude" | string;
  /** the boolean field that means "flagged" (e.g. is_noise), or null */
  flag: string | null;
  origin: "core" | "owner" | "agent" | string;
  status: string;
  active_annotations: number;
  /** how many classifiers write it (a count on current servers) */
  classifiers: number | string[];
}

export interface ClassifierDetail extends Classifier {
  code?: string | null;
  runs: ClassifierRun[];
  attribute_definition: AnnotationAttribute | null;
}

export interface Mark {
  target: "thread" | "participant" | "message";
  key: string | null;
  label?: string | null;
  /** null on a note: a verdict recorded without writing on messages */
  value: Record<string, unknown> | null;
  detail?: Record<string, unknown> | string | null;
  confidence?: number | null;
  reason?: string | null;
}

export interface JudgedParticipant {
  key: string;
  label: string | null;
  is_owner: boolean;
  turns: number;
}

export interface PythonJudgement {
  status: "done" | "error" | "skipped";
  verdict?: string | null;
  marks?: Mark[];
  participants?: JudgedParticipant[];
  messages?: number;
  error?: string | null;
  logs?: string | string[] | null;
}

export interface PythonResultItem {
  thread_id: string | number;
  title: string | null;
  channel: string;
  started_at: string | null;
  judged_at: string | null;
  version: number;
  result: PythonJudgement;
}

export interface PythonResults {
  kind: "python";
  verdicts: Record<string, number>;
  items: PythonResultItem[];
}

export interface MessageResultItem {
  annotation_id: number | string;
  message_id: number | string;
  value: Record<string, unknown> | null;
  confidence: number | null;
  reason: string | null;
  created_at: string;
  status: string;
  happened_at: string | null;
  content: string | null;
  channel: string;
  author_handle: string | null;
}

export interface MessageResults {
  kind: "message";
  items: MessageResultItem[];
}

export type ClassifierResults = PythonResults | MessageResults;

export interface CheckResult {
  ok: boolean;
  errors: string[];
  warnings: string[];
  grain?: string;
  kind?: ClassifierKind;
}

export interface NewAttribute {
  name: string;
  description: string;
  value_schema: ValueSchema;
  recall_effect: "none" | "exclude";
  /** name of the boolean field that means "flagged" */
  flag?: string;
}

export interface ClassifierDefinition {
  name: string;
  kind: ClassifierKind;
  description?: string;
  attribute: string | NewAttribute;
  channels: string[];
  spec: Record<string, unknown>;
  code?: string;
  monthly_cap_usd?: number;
  backfill?: string;
}

export interface DefineResult {
  classifier: Classifier;
  check: CheckResult;
  status: ClassifierStatus;
  receipt: unknown;
}

export type TrySample = { limit: number } | { thread_ids: Array<string | number> } | { message_ids: Array<string | number> };

export interface PythonTryItem {
  thread_id: string | number;
  title: string | null;
  status: "done" | "error" | "skipped" | string;
  verdict?: string | null;
  marks?: Mark[];
  participants?: JudgedParticipant[];
  would_cover?: { messages: number; of: number } | null;
  logs?: string | string[] | null;
  error?: string | null;
}

export interface MessageTryItem {
  message_id: string | number;
  channel: string;
  content: string | null;
  author: string | null;
  status: "done" | "error" | "held" | "stopped" | string;
  value: Record<string, unknown> | null;
  confidence: number | null;
  reason: string | null;
  error?: string | null;
}

export interface TryResult {
  ok: boolean;
  items: Array<PythonTryItem | MessageTryItem>;
  calls: number;
  cost: number;
  cost_per_item: number | null;
  projection: { threads_to_judge?: number; messages_to_judge?: number; estimated_cost: number } | null;
}

export interface DraftResult {
  definition: Partial<ClassifierDefinition>;
  check: CheckResult | null;
  notes: string | string[] | null;
  model: string | null;
}

export interface GuideMessageExample {
  title: string;
  definition: Partial<ClassifierDefinition>;
}
export interface GuidePythonExample {
  title: string;
  attribute: string | NewAttribute;
  channels: string[];
  code: string;
}

export interface ClassifierGuide {
  summary: string;
  workflow: string[];
  message_spec: Record<string, unknown>;
  python: {
    contract: string;
    allowed_imports: string[];
    not_available?: string[];
    spec: Record<string, unknown>;
    rules: string[];
  };
  examples: { message: GuideMessageExample[]; python: GuidePythonExample[] };
}

export interface ClassificationParticipant {
  key: string;
  label: string | null;
  is_owner: boolean;
  person_id: number | null;
  turns: number;
  flagged: number;
}

export interface ClassificationMessage {
  id: number | string;
  at: string;
  offset_s: number | null;
  participant: string | null;
  text: string | null;
  annotation: {
    value: Record<string, unknown> | null;
    method: "owner" | "rule" | "model" | string;
    group_annotation_id: number | string | null;
    job: string | null;
  } | null;
}

export interface GroupAnnotation {
  id: number | string;
  target_kind: "thread" | "participant";
  participant_key: string | null;
  value: Record<string, unknown> | null;
  detail: Record<string, unknown> | string | null;
  method: "owner" | "rule" | "model" | string;
  confidence: number | null;
  reason: string | null;
  messages_covered: number;
}

export interface ThreadClassification {
  thread: { id: string | number; channel: string; title: string | null; started_at: string | null; duration_s: number | null };
  attribute: string;
  participants: ClassificationParticipant[];
  messages: ClassificationMessage[];
  messages_total: number;
  flagged_total: number;
  group_annotations: GroupAnnotation[];
  verdicts: Array<{ name: string; kind: ClassifierKind; result: PythonJudgement | Record<string, unknown>; judged_at: string; version: number }>;
}

export interface GroupAnnotationInput {
  thread_id: string | number;
  attribute: string;
  participant_key?: string;
  value: Record<string, unknown> | null;
  detail?: Record<string, unknown>;
  reason?: string;
}

/* ---- helpers ------------------------------------------------------------- */

export function idempotencyKey(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, { method: "POST", body, headers: { "Idempotency-Key": idempotencyKey() } });
}

const enc = encodeURIComponent;

/* ---- queries ------------------------------------------------------------- */

export const CLASSIFIERS_KEY = ["classifiers"] as const;

export function useClassifiers(enabled = true): UseQueryResult<{ classifiers: Classifier[]; sandbox: string }> {
  return useQuery({
    queryKey: [...CLASSIFIERS_KEY, "list"],
    queryFn: ({ signal }) => request<{ classifiers: Classifier[]; sandbox: string }>("/classifiers", { signal }),
    enabled,
    retry: false,
    staleTime: 30_000,
  });
}

export function useClassifier(name: string | undefined): UseQueryResult<ClassifierDetail> {
  return useQuery({
    queryKey: [...CLASSIFIERS_KEY, "detail", name],
    queryFn: ({ signal }) => request<ClassifierDetail>(`/classifiers/${enc(name as string)}`, { signal }),
    enabled: !!name,
    retry: false,
    staleTime: 30_000,
  });
}

export interface ResultsQuery {
  limit?: number;
  offset?: number;
  verdict?: string | null;
}

export function useClassifierResults(name: string | undefined, q: ResultsQuery = {}): UseQueryResult<ClassifierResults> {
  const params = new URLSearchParams();
  if (q.limit) params.set("limit", String(q.limit));
  if (q.offset) params.set("offset", String(q.offset));
  if (q.verdict) params.set("verdict", q.verdict);
  const qs = params.toString();
  return useQuery({
    queryKey: [...CLASSIFIERS_KEY, "results", name, q.limit ?? null, q.offset ?? 0, q.verdict ?? null],
    queryFn: ({ signal }) =>
      request<ClassifierResults>(`/classifiers/${enc(name as string)}/results${qs ? `?${qs}` : ""}`, { signal }),
    enabled: !!name,
    retry: false,
    placeholderData: (prev) => prev,
  });
}

export function useClassifierGuide(enabled = true): UseQueryResult<ClassifierGuide> {
  return useQuery({
    queryKey: [...CLASSIFIERS_KEY, "guide"],
    queryFn: ({ signal }) => request<ClassifierGuide>("/classifiers/guide", { signal }),
    enabled,
    retry: false,
    staleTime: 10 * 60_000,
  });
}

export function useAnnotationAttributes(enabled = true): UseQueryResult<{ attributes: AnnotationAttribute[] }> {
  return useQuery({
    queryKey: ["annotation-attributes"],
    queryFn: ({ signal }) => request<{ attributes: AnnotationAttribute[] }>("/annotation-attributes", { signal }),
    enabled,
    retry: false,
    staleTime: 5 * 60_000,
  });
}

export function useThreadClassification(
  threadId: string | undefined,
  attribute: string | undefined,
  classifier: string | undefined,
): UseQueryResult<ThreadClassification> {
  const params = new URLSearchParams();
  if (attribute) params.set("attribute", attribute);
  if (classifier) params.set("classifier", classifier);
  return useQuery({
    queryKey: ["thread-classification", threadId, attribute, classifier],
    queryFn: ({ signal }) =>
      request<ThreadClassification>(`/threads/${enc(threadId as string)}/classification?${params.toString()}`, { signal }),
    enabled: !!threadId && !!attribute,
    retry: false,
  });
}

/** Validate a definition; read-only on the server, so it is safe to run live. */
export function checkDefinition(definition: ClassifierDefinition, signal?: AbortSignal): Promise<CheckResult> {
  return request<CheckResult>("/classifiers/check", { method: "POST", body: { definition }, signal });
}

export function useCheck(definition: ClassifierDefinition | null): UseQueryResult<CheckResult> {
  const key = definition ? JSON.stringify(definition) : null;
  return useQuery({
    queryKey: [...CLASSIFIERS_KEY, "check", key],
    queryFn: ({ signal }) => checkDefinition(definition as ClassifierDefinition, signal),
    enabled: !!definition,
    retry: false,
    staleTime: Infinity,
    placeholderData: (prev) => prev,
  });
}

/* ---- writes -------------------------------------------------------------- */

export const defineClassifier = (definition: ClassifierDefinition) => post<DefineResult>("/classifiers", definition);
export const tryClassifier = (body: { definition: ClassifierDefinition } | { name: string }, sample: TrySample) =>
  post<TryResult>("/classifiers/try", { ...body, sample });
export const draftClassifier = (body: { goal: string; kind?: "auto" | ClassifierKind; channels?: string[]; attribute?: string }) =>
  post<DraftResult>("/classifiers/draft", body);
export const setClassifierStatus = (name: string, status: "active" | "paused" | "rejected") =>
  post<Classifier>(`/classifiers/${enc(name)}/status`, { status });
export const runClassifier = (name: string, maxItems?: number) =>
  post<ClassifierRun>(`/classifiers/${enc(name)}/run`, maxItems ? { max_items: maxItems } : {});
export const backfillClassifier = (name: string, since: string) =>
  post<unknown>(`/classifiers/${enc(name)}/backfill`, { since });
export const retractClassifierMarks = (name: string) => post<unknown>(`/classifiers/${enc(name)}/retract-marks`, {});
export const createGroupAnnotation = (input: GroupAnnotationInput) => post<GroupAnnotation>("/group-annotations", input);
export const retractGroupAnnotation = (id: string | number) => post<unknown>(`/group-annotations/${enc(String(id))}/retract`, {});

/** Invalidate everything classifier-shaped after a write. */
export function useInvalidateClassifiers(): () => Promise<void> {
  const qc = useQueryClient();
  return async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: CLASSIFIERS_KEY }),
      qc.invalidateQueries({ queryKey: ["annotation-attributes"] }),
      qc.invalidateQueries({ queryKey: ["thread-classification"] }),
    ]);
  };
}

export function useDefineClassifier() {
  const invalidate = useInvalidateClassifiers();
  return useMutation({ mutationFn: defineClassifier, onSuccess: () => invalidate() });
}
