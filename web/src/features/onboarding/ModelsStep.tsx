import { useMemo, useState, type ReactNode } from "react";
import { Search, Bot, Check, Ban, ShieldCheck, CircleDollarSign, Library } from "lucide-react";
import { Skeleton, Field, Select, Input, Button, ErrorState } from "@/ui";
import {
  useProviders,
  useSaveProvider,
  useSaveRoles,
  testProvider,
  setupProblem,
  type ProviderStatus,
  type ProviderRole,
  type CheckResult,
  type SetupProblem,
} from "@/api/setup";
import { StepHeader, WizardFooter } from "./parts";
import { PluginIcon } from "./PluginIcon";
import { FieldGroup, fromProviderField } from "./FieldGroup";
import { CheckResultView } from "./CheckResultView";
import { ProblemView } from "./ProblemView";
import styles from "./Onboarding.module.css";

export function ModelsStep(): ReactNode {
  const providers = useProviders();

  return (
    <div className={styles.step}>
      <StepHeader
        eyebrow="Intelligence"
        title="Give your hub a sense of meaning"
        lede="Three optional roles. Embeddings let you search by meaning. An assistant answers you in plain language. The librarian keeps your lenses filled and writes your pages. Pick a provider for each, or skip — you can change these any time."
      />

      <div className={styles.body}>
        {providers.isLoading ? (
          <div className={styles.loadingStack}>
            <Skeleton height={120} />
            <Skeleton height={120} />
          </div>
        ) : providers.error ? (
          <ErrorState error={providers.error} title="Could not load providers" onRetry={() => void providers.refetch()} />
        ) : providers.data ? (
          <>
            <RoleBlock
              role="embedding"
              title="Embeddings"
              sub="Meaning search"
              icon={<Search size={16} />}
              providers={providers.data.providers}
              current={providers.data.roles.embedding}
              skipLabel="Keyword search only"
            />
            <RoleBlock
              role="assistant"
              title="Assistant"
              sub="Language & reasoning"
              icon={<Bot size={16} />}
              providers={providers.data.providers}
              current={providers.data.roles.assistant}
              skipLabel="Skip for now"
            />
            <RoleBlock
              role="semantic"
              title="Librarian, lenses & pages"
              sub="Keeps lenses filled and writes pages — a CLI proxy or local model makes it free per call"
              icon={<Library size={16} />}
              providers={providers.data.providers}
              current={providers.data.roles.semantic}
              skipLabel="Skip for now"
            />
          </>
        ) : null}
      </div>

      <WizardFooter continueLabel="Continue" />
    </div>
  );
}

function RoleBlock({
  role,
  title,
  sub,
  icon,
  providers,
  current,
  skipLabel,
}: {
  role: ProviderRole;
  title: string;
  sub: string;
  icon: ReactNode;
  providers: ProviderStatus[];
  current: { provider: string | null; model?: string | null };
  skipLabel: string;
}): ReactNode {
  const saveRoles = useSaveRoles();
  const eligible = useMemo(() => providers.filter((p) => p.roles.includes(role)), [providers, role]);
  const [selected, setSelected] = useState<string | null>(current.provider);

  const defaultModel = (p: ProviderStatus): string | null => {
    const list = p.models[role] ?? [];
    return list.find((m) => m.default)?.id ?? list[0]?.id ?? null;
  };

  const pick = (p: ProviderStatus | null): void => {
    if (!p) {
      setSelected(null);
      saveRoles.mutate({ [role]: { provider: null } });
      return;
    }
    setSelected(p.id);
    saveRoles.mutate({ [role]: { provider: p.id, model: current.provider === p.id ? current.model ?? defaultModel(p) : defaultModel(p) } });
  };

  const selectedProvider = eligible.find((p) => p.id === selected) ?? null;

  return (
    <div className={styles.roleBlock}>
      <div className={styles.roleHead}>
        <span className={styles.roleIcon}>{icon}</span>
        <div>
          <div className={styles.roleTitle}>{title}</div>
          <div className={styles.roleSub}>{sub}</div>
        </div>
      </div>

      <div className={styles.providerGrid} role="radiogroup" aria-label={`${title} provider`}>
        {eligible.map((p) => (
          <button
            key={p.id}
            type="button"
            role="radio"
            aria-checked={selected === p.id}
            className={styles.providerCard}
            data-selected={selected === p.id}
            onClick={() => pick(p)}
          >
            <span className={styles.providerMark}>
              <PluginIcon name={providerIcon(p.id)} size={16} />
            </span>
            <span style={{ flex: 1, minWidth: 0 }}>
              <span className={styles.providerName}>{p.name}</span>
              <span className={styles.providerTag}>{p.configured ? "configured" : p.tagline}</span>
            </span>
            {selected === p.id && <Check size={15} style={{ color: "var(--brass)" }} aria-hidden />}
          </button>
        ))}
        <button
          type="button"
          role="radio"
          aria-checked={selected === null}
          className={styles.providerCard}
          data-selected={selected === null}
          onClick={() => pick(null)}
        >
          <span className={styles.providerMark}>
            <Ban size={16} />
          </span>
          <span style={{ flex: 1, minWidth: 0 }}>
            <span className={styles.providerName}>Skip</span>
            <span className={styles.providerTag}>{skipLabel}</span>
          </span>
          {selected === null && <Check size={15} style={{ color: "var(--brass)" }} aria-hidden />}
        </button>
      </div>

      {selectedProvider && <ProviderConfig provider={selectedProvider} role={role} model={current.provider === selectedProvider.id ? current.model ?? null : defaultModel(selectedProvider)} />}
    </div>
  );
}

function ProviderConfig({ provider, role, model }: { provider: ProviderStatus; role: ProviderRole; model: string | null }): ReactNode {
  const saveProvider = useSaveProvider();
  const saveRoles = useSaveRoles();
  const [values, setValues] = useState<Record<string, string>>({});
  const [check, setCheck] = useState<CheckResult | null>(null);
  const [problem, setProblem] = useState<SetupProblem | null>(null);
  const [testing, setTesting] = useState(false);

  const [discovered, setDiscovered] = useState<string[]>([]);
  const [typedModel, setTypedModel] = useState(model ?? "");

  const fields = useMemo(
    () => provider.fields.map((f) => fromProviderField(f, provider.locked.includes(f.key))),
    [provider.fields, provider.locked],
  );
  const models = provider.models[role] ?? [];
  const saveModel = (id: string): void => { if (id.trim()) saveRoles.mutate({ [role]: { provider: provider.id, model: id.trim() } }); };

  const payload = (): Record<string, string> => {
    const out: Record<string, string> = {};
    for (const f of provider.fields) {
      if (provider.locked.includes(f.key)) continue;
      const v = values[f.key] ?? "";
      if (f.kind === "secret" && v === "") continue;
      if (v === "") continue;
      out[f.key] = v;
    }
    return out;
  };

  const doTest = async (): Promise<void> => {
    setTesting(true);
    setProblem(null);
    setCheck(null);
    try {
      const r = await testProvider(provider.id, payload());
      setCheck(r);
      // Providers with no fixed model list (cli_proxy / local) report the
      // server's models in detail.models after a successful connection.
      const dm = (r.detail?.models as unknown);
      if (r.ok && Array.isArray(dm)) setDiscovered(dm.filter((m): m is string => typeof m === "string"));
    } catch (e) {
      setProblem(setupProblem(e));
    } finally {
      setTesting(false);
    }
  };

  const doSave = (): void => {
    setProblem(null);
    const p = payload();
    if (Object.keys(p).length > 0) saveProvider.mutate({ id: provider.id, values: p }, { onError: (e) => setProblem(setupProblem(e)) });
  };

  return (
    <div className={styles.providerConfig}>
      {fields.length > 0 && <FieldGroup fields={fields} values={values} onChange={(k, v) => { setValues((s) => ({ ...s, [k]: v })); setCheck(null); }} masked={provider.values} />}

      {models.length > 0 ? (
        <Field label="Model" htmlFor={`${provider.id}-model`} className={styles.confidence}>
          <Select
            id={`${provider.id}-model`}
            value={model ?? ""}
            onChange={(e) => saveModel(e.target.value)}
          >
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
                {m.note ? ` — ${m.note}` : ""}
              </option>
            ))}
          </Select>
        </Field>
      ) : (
        <Field
          label="Model"
          htmlFor={`${provider.id}-model`}
          className={styles.confidence}
          hint={discovered.length > 0 ? "Pick one the server reported, or type a model id" : "Test the connection to list the server's models, or type a model id"}
        >
          <Input
            id={`${provider.id}-model`}
            mono
            list={`${provider.id}-models`}
            value={typedModel}
            placeholder="model id"
            onChange={(e) => setTypedModel(e.target.value)}
            onBlur={() => saveModel(typedModel)}
          />
          {discovered.length > 0 && (
            <datalist id={`${provider.id}-models`}>
              {discovered.map((m) => <option key={m} value={m} />)}
            </datalist>
          )}
        </Field>
      )}

      {(provider.cost_note || provider.privacy_note) && (
        <div className={styles.noteRow}>
          {provider.cost_note && (
            <span>
              <CircleDollarSign size={13} /> {provider.cost_note}
            </span>
          )}
          {provider.privacy_note && (
            <span>
              <ShieldCheck size={13} /> {provider.privacy_note}
            </span>
          )}
        </div>
      )}

      {check && <div style={{ marginTop: "var(--s-3)" }}><CheckResultView result={check} /></div>}
      {problem && <div style={{ marginTop: "var(--s-3)" }}><ProblemView problem={problem} /></div>}

      <div className={styles.cardActions} style={{ marginTop: "var(--s-4)" }}>
        <span className={styles.footerSpacer} />
        <Button variant="ghost" onClick={() => void doTest()} disabled={testing}>
          {testing ? "Testing…" : "Test connection"}
        </Button>
        <Button variant="primary" onClick={doSave} disabled={saveProvider.isPending}>
          {saveProvider.isPending ? "Saving…" : "Save"}
        </Button>
      </div>
    </div>
  );
}

const PROVIDER_ICONS: Record<string, string> = {
  voyage: "compass",
  openrouter: "route",
  openai: "sparkles",
  anthropic: "sparkles",
  gemini: "gem",
  cli_proxy: "terminal",
  local: "server",
  immich: "image",
};
function providerIcon(id: string): string {
  return PROVIDER_ICONS[id] ?? "cpu";
}
