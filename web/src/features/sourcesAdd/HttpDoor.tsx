/*
 * Push over HTTP: build a channel, get real curl/Python/Node to paste, and test
 * it with a dry run against this very server. The snippets use the real endpoint
 * paths and payload shapes (map-http 9a); the tester registers the channel and
 * dry-runs one record, rolled back, so nothing is written.
 */
import { useMemo, useState, type ReactNode } from "react";
import { Plus, Trash2, Loader2, CheckCircle2, AlertTriangle } from "lucide-react";
import { ApiError } from "@/api/errors";
import { Button, CodeBlock, Field, IconButton, Input, Segmented, Select } from "@/ui";
import type { FieldType } from "@/lens";
import { snippetFor, type ChannelDraft, type SnippetLang } from "./snippets";
import styles from "./Add.module.css";

const FIELD_TYPES: FieldType[] = ["text", "integer", "number", "boolean", "timestamp", "json"];

interface DraftField {
  name: string;
  type: FieldType;
  description: string;
}

export function HttpDoor(): ReactNode {
  const [name, setName] = useState("import.my_source");
  const [description, setDescription] = useState("");
  const [fields, setFields] = useState<DraftField[]>([{ name: "rating", type: "integer", description: "" }]);
  const [lang, setLang] = useState<SnippetLang>("curl");
  const [token, setToken] = useState("");
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  const draft: ChannelDraft = useMemo(
    () => ({
      name,
      plugin: "webimport",
      description: description || "Pushed over HTTP",
      fields: fields.filter((f) => /^[a-z][a-z0-9_]*$/.test(f.name)),
    }),
    [name, description, fields],
  );

  const snippet = useMemo(() => snippetFor(lang, draft, { baseUrl: location.origin }), [lang, draft]);

  const setField = (i: number, patch: Partial<DraftField>): void => {
    setFields((fs) => fs.map((f, j) => (j === i ? { ...f, ...patch } : f)));
  };

  const test = async (): Promise<void> => {
    setTesting(true);
    setResult(null);
    const tok = token.trim();
    const headers: Record<string, string> = {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-Throughline-Client": "web",
    };
    if (tok) headers.Authorization = `Bearer ${tok}`;
    try {
      const chanRes = await fetch(`/v1/channels`, {
        method: "POST",
        headers,
        credentials: "same-origin",
        body: JSON.stringify({
          name: draft.name,
          plugin: draft.plugin,
          description: draft.description,
          fields: draft.fields.map((f) => ({ name: f.name, type: f.type, description: f.description || undefined })),
        }),
      });
      if (!chanRes.ok) throw new ApiError({ status: chanRes.status, message: detailOf(await chanRes.json().catch(() => ({}))) });
      const sampleFields: Record<string, unknown> = {};
      for (const f of draft.fields) sampleFields[f.name] = sampleFor(f.type);
      const ingRes = await fetch(`/v1/ingest/messages`, {
        method: "POST",
        headers: { ...headers, "Idempotency-Key": `httptest-${Date.now()}` },
        credentials: "same-origin",
        body: JSON.stringify({
          plugin: draft.plugin,
          dry_run: true,
          messages: [
            {
              channel: draft.name,
              external_id: "test-1",
              thread: { external_id: "test" },
              happened_at: "2024-01-01T12:00:00+00:00",
              content: "a test record",
              fields: sampleFields,
              payload: { test: true },
            },
          ],
        }),
      });
      const json = await ingRes.json().catch(() => ({}));
      if (!ingRes.ok) throw new ApiError({ status: ingRes.status, message: detailOf(json) });
      setResult({
        ok: true,
        text: `Dry run OK — received ${json.received}, would write ${json.inserted + json.new_versions}, rejected ${(json.rejected ?? []).length}. Nothing was written.`,
      });
    } catch (e) {
      setResult({ ok: false, text: e instanceof ApiError ? `${e.message} (HTTP ${e.status})` : String((e as Error).message ?? e) });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className={styles.httpDoor}>
      <div className={styles.twoCol}>
        <div className={styles.mapCol}>
          <section className={styles.mapSection}>
            <h3 className={styles.mapHeading}>Channel</h3>
            <Field label="Name" hint="Dot.separated lower_snake_case.">
              <Input value={name} spellCheck={false} mono onChange={(e) => setName(e.target.value)} aria-label="Channel name" />
            </Field>
            <Field label="Description">
              <Input value={description} onChange={(e) => setDescription(e.target.value)} aria-label="Channel description" />
            </Field>
          </section>

          <section className={styles.mapSection}>
            <h3 className={styles.mapHeading}>Fields</h3>
            <div className={styles.fieldTable}>
              {fields.map((f, i) => (
                <div key={i} className={styles.fieldRow}>
                  <Input
                    className={styles.fieldNameInput}
                    value={f.name}
                    spellCheck={false}
                    placeholder="name"
                    onChange={(e) => setField(i, { name: e.target.value })}
                    aria-label={`Field ${i + 1} name`}
                  />
                  <Select value={f.type} onChange={(e) => setField(i, { type: e.target.value as FieldType })} aria-label={`Field ${i + 1} type`}>
                    {FIELD_TYPES.map((t) => (
                      <option key={t} value={t}>
                        {t}
                      </option>
                    ))}
                  </Select>
                  <Input
                    className={styles.fieldDescInput}
                    value={f.description}
                    placeholder="description"
                    onChange={(e) => setField(i, { description: e.target.value })}
                    aria-label={`Field ${i + 1} description`}
                  />
                  <IconButton label="Remove field" size="sm" onClick={() => setFields((fs) => fs.filter((_, j) => j !== i))}>
                    <Trash2 size={14} />
                  </IconButton>
                </div>
              ))}
            </div>
            <Button size="sm" icon={<Plus size={14} />} onClick={() => setFields((fs) => [...fs, { name: "", type: "text", description: "" }])}>
              Add field
            </Button>
          </section>

          <section className={styles.mapSection}>
            <h3 className={styles.mapHeading}>Test it</h3>
            <p className={styles.mapNote}>Registers the channel and dry-runs one record against this server. Paste a token with write access, or leave blank to use your signed-in token.</p>
            <Field label="Token (optional)" hint="Used only for this test; never saved.">
              <Input type="password" value={token} spellCheck={false} placeholder="plugin or owner token" onChange={(e) => setToken(e.target.value)} aria-label="Test token" />
            </Field>
            <Button variant="default" onClick={() => void test()} disabled={testing} icon={testing ? <Loader2 size={14} className={styles.spin} /> : undefined}>
              {testing ? "Testing…" : "Run dry-run test"}
            </Button>
            {result ? (
              <p className={result.ok ? styles.testOk : styles.runnerError} role="status">
                {result.ok ? <CheckCircle2 size={14} aria-hidden /> : <AlertTriangle size={14} aria-hidden />} {result.text}
              </p>
            ) : null}
          </section>
        </div>

        <div className={styles.previewCol}>
          <div className={styles.snippetHead}>
            <Segmented
              value={lang}
              onChange={(v) => setLang(v)}
              items={[
                { value: "curl" as SnippetLang, label: "curl" },
                { value: "python" as SnippetLang, label: "Python" },
                { value: "node" as SnippetLang, label: "Node" },
              ]}
              aria-label="Snippet language"
            />
          </div>
          <CodeBlock code={snippet} lang={lang} wrap />
        </div>
      </div>
    </div>
  );
}

function sampleFor(type: FieldType): unknown {
  switch (type) {
    case "integer":
      return 1;
    case "number":
      return 1.5;
    case "boolean":
      return true;
    case "timestamp":
      return "2024-01-01T12:00:00+00:00";
    case "json":
      return { k: "v" };
    default:
      return "example";
  }
}
function detailOf(json: unknown): string {
  const d = (json as { detail?: unknown })?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => (x as { msg?: string }).msg ?? JSON.stringify(x)).join("; ");
  return "request rejected";
}
