/*
 * Validate and import through the real contract. First register the channel and
 * dry-run a sample (the server's own validation, rolled back). Then, on confirm,
 * ingest every row in idempotent batches with a live count, retrying a failed
 * batch, and finish with a link to the new channel page.
 *
 * Writes need a token that can write. An unrestricted owner token (the usual web
 * token) can; if it cannot, we say exactly what to run on the box to mint an
 * import token and accept it for this session only — kept in memory, never saved.
 */
import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { CheckCircle2, AlertTriangle, Loader2, ArrowRight } from "lucide-react";
import { request, useBootstrap } from "@/api";
import { ApiError } from "@/api/errors";
import { Button, CodeBlock, Field, Input } from "@/ui";
import type { ParseResult } from "./parse";
import { buildChannelBody, buildMessages, type EnvelopeMessage, type Mapping } from "./envelope";
import styles from "./Add.module.css";

const PLUGIN = "webimport";
const BATCH = 400;
const DRY_SAMPLE = 100;

type Phase = "idle" | "validating" | "validated" | "importing" | "done" | "error";

interface DryResult {
  received: number;
  wouldInsert: number;
  rejected: Array<{ index: number; external_id: string; error: string }>;
}

interface Progress {
  done: number;
  total: number;
  inserted: number;
  updated: number;
  unchanged: number;
  rejected: number;
}

export interface IngestRunnerProps {
  mapping: Mapping;
  parsed: ParseResult;
  disabled: boolean;
  onBeforeImport: () => void;
}

export function IngestRunner({ mapping, parsed, disabled, onBeforeImport }: IngestRunnerProps): ReactNode {
  const { data: boot } = useBootstrap();
  const [phase, setPhase] = useState<Phase>("idle");
  const [dry, setDry] = useState<DryResult | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sessionToken, setSessionToken] = useState("");
  const [showToken, setShowToken] = useState(false);

  const canWrite = boot?.token.can_write ?? false;
  const effectiveToken = sessionToken.trim();

  // POST to an ingest/channel route, optionally as a pasted session token.
  async function post<T>(path: string, body: unknown, idemKey?: string): Promise<T> {
    const headers: Record<string, string> = {};
    if (idemKey) headers["Idempotency-Key"] = idemKey;
    if (effectiveToken) {
      // A session token overrides the stored one — a raw same-origin fetch.
      const res = await fetch(`/v1${path}`, {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-Throughline-Client": "web",
          Authorization: `Bearer ${effectiveToken}`,
          ...headers,
        },
        body: JSON.stringify(body),
        credentials: "same-origin",
      });
      const json = await res.json().catch(() => ({}));
      if (!res.ok) throw new ApiError({ status: res.status, message: detailOf(json), detail: detailOf(json) });
      return json as T;
    }
    return request<T>(path, { method: "POST", body, headers });
  }

  const validate = async (): Promise<void> => {
    setPhase("validating");
    setError(null);
    setDry(null);
    try {
      await post("/channels", buildChannelBody(mapping, PLUGIN));
    } catch (e) {
      setError(`Could not register the channel. ${messageOf(e)}`);
      setPhase("error");
      return;
    }
    const { messages } = buildMessages(mapping, parsed.table.rows);
    const sample = messages.slice(0, DRY_SAMPLE);
    try {
      const r = await post<IngestResponse>("/ingest/messages", {
        plugin: PLUGIN,
        dry_run: true,
        messages: sample,
      });
      setDry({
        received: r.received,
        wouldInsert: r.inserted + r.new_versions,
        rejected: (r.rejected ?? []).slice(0, 8),
      });
      setPhase("validated");
    } catch (e) {
      setError(`Dry run failed. ${messageOf(e)}`);
      setPhase("error");
    }
  };

  const runImport = async (): Promise<void> => {
    onBeforeImport();
    setPhase("importing");
    setError(null);
    const { messages } = buildMessages(mapping, parsed.table.rows);
    const base = `imp-${mapping.channelName.replace(/[^a-z0-9]/gi, "")}-${Date.now()}`;
    const prog: Progress = { done: 0, total: messages.length, inserted: 0, updated: 0, unchanged: 0, rejected: 0 };
    setProgress({ ...prog });

    for (let i = 0; i < messages.length; i += BATCH) {
      const batch = messages.slice(i, i + BATCH);
      const key = `${base}-b${i / BATCH}`.slice(0, 180);
      let ok = false;
      let lastErr: unknown = null;
      for (let attempt = 0; attempt < 3 && !ok; attempt++) {
        try {
          const r = await post<IngestResponse>(
            "/ingest/messages",
            { plugin: PLUGIN, dry_run: false, messages: batch },
            key,
          );
          prog.inserted += r.inserted;
          prog.updated += r.new_versions;
          prog.unchanged += r.unchanged;
          prog.rejected += (r.rejected ?? []).length;
          ok = true;
        } catch (e) {
          lastErr = e;
          await sleep(300 * (attempt + 1));
        }
      }
      if (!ok) {
        setError(`A batch failed after retries. ${messageOf(lastErr)} Imported ${prog.inserted} so far.`);
        setPhase("error");
        return;
      }
      prog.done = Math.min(messages.length, i + batch.length);
      setProgress({ ...prog });
    }
    setPhase("done");
  };

  const total = parsed.table.rows.length;

  return (
    <div className={styles.runner}>
      <div className={styles.runnerHead}>
        <h3 className={styles.mapHeading}>Validate &amp; import</h3>
        {!canWrite && !effectiveToken ? (
          <span className={styles.tokenWarn}>this token cannot write — paste an import token below</span>
        ) : null}
      </div>

      {!canWrite ? (
        <div className={styles.tokenBox}>
          <p className={styles.mapNote}>
            Importing writes records, which needs a plugin/import token. Tokens are only ever minted on the box — run
            this there, then paste the token (it stays in memory for this import only, never saved):
          </p>
          <CodeBlock code={`throughline token create webimport --kind plugin --plugin ${PLUGIN}`} lang="bash" />
          <Field label="Import token" hint="Kept in memory for this session only.">
            <Input
              type={showToken ? "text" : "password"}
              value={sessionToken}
              spellCheck={false}
              placeholder="paste the token from the box"
              onChange={(e) => setSessionToken(e.target.value)}
              aria-label="Import token"
            />
          </Field>
          <label className={styles.inlineCheck}>
            <input type="checkbox" checked={showToken} onChange={(e) => setShowToken(e.target.checked)} /> show token
          </label>
        </div>
      ) : null}

      <div className={styles.runnerActions}>
        <Button
          variant="default"
          onClick={() => void validate()}
          disabled={disabled || phase === "validating" || phase === "importing"}
          icon={phase === "validating" ? <Loader2 className={styles.spin} size={15} /> : undefined}
        >
          {phase === "validating" ? "Validating…" : "Dry run"}
        </Button>
        <Button
          variant="primary"
          onClick={() => void runImport()}
          disabled={disabled || phase !== "validated" || (!canWrite && !effectiveToken)}
          icon={phase === "importing" ? <Loader2 className={styles.spin} size={15} /> : undefined}
        >
          {phase === "importing" ? "Importing…" : `Import ${total.toLocaleString()} records`}
        </Button>
      </div>

      {error ? (
        <p className={styles.runnerError} role="alert">
          <AlertTriangle size={14} aria-hidden /> {error}
        </p>
      ) : null}

      {dry && phase === "validated" ? (
        <div className={styles.dryResult}>
          <p>
            <CheckCircle2 size={14} aria-hidden className={styles.okIcon} /> Dry run of {dry.received} sample records:{" "}
            <strong>{dry.wouldInsert}</strong> would be written
            {dry.rejected.length ? `, ${dry.rejected.length} rejected` : ", none rejected"}.
          </p>
          {dry.rejected.length ? (
            <pre className={styles.rejectPre}>
              {dry.rejected.map((r) => `row ${r.index} (${r.external_id || "—"}): ${firstLine(r.error)}`).join("\n")}
            </pre>
          ) : null}
        </div>
      ) : null}

      {progress && (phase === "importing" || phase === "error") ? (
        <div className={styles.progressWrap}>
          <div className={styles.progressBar}>
            <div
              className={styles.progressFill}
              style={{ width: `${progress.total ? (progress.done / progress.total) * 100 : 0}%` }}
            />
          </div>
          <p className={styles.progressText}>
            {progress.done.toLocaleString()} / {progress.total.toLocaleString()} · {progress.inserted.toLocaleString()}{" "}
            new · {progress.updated.toLocaleString()} updated · {progress.unchanged.toLocaleString()} unchanged
            {progress.rejected ? ` · ${progress.rejected} rejected` : ""}
          </p>
        </div>
      ) : null}

      {phase === "done" && progress ? (
        <div className={styles.doneBox}>
          <p className={styles.doneLine}>
            <CheckCircle2 size={16} aria-hidden className={styles.okIcon} /> Imported {progress.inserted.toLocaleString()}{" "}
            records into <code>{mapping.channelName}</code>.
          </p>
          <Link to={`/c/${encodeURIComponent(mapping.channelName)}`} className={styles.doneLink}>
            Open the channel page <ArrowRight size={15} aria-hidden />
          </Link>
        </div>
      ) : null}
    </div>
  );
}

interface IngestResponse {
  received: number;
  inserted: number;
  new_versions: number;
  unchanged: number;
  rejected?: Array<{ index: number; external_id: string; error: string }>;
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
function messageOf(e: unknown): string {
  if (e instanceof ApiError) return e.message || `HTTP ${e.status}`;
  return String((e as Error)?.message ?? e ?? "");
}
function detailOf(json: unknown): string {
  const d = (json as { detail?: unknown })?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => (x as { msg?: string }).msg ?? JSON.stringify(x)).join("; ");
  return "request rejected";
}
function firstLine(s: string): string {
  return (s || "").split("\n")[0] ?? s;
}
