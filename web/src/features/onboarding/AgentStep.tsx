import { useMemo, useState, type ReactNode } from "react";
import { Copy, KeyRound, Wand2, TriangleAlert, Check } from "lucide-react";
import { Skeleton, Tabs, Button, CodeBlock, IconButton, Dialog, ErrorState } from "@/ui";
import {
  useAgentView,
  mintAgentToken,
  getSetupPrompt,
  setupProblem,
  type AgentClient,
  type AgentToken,
  type SetupProblem,
} from "@/api/setup";
import { StepHeader, WizardFooter, Callout } from "./parts";
import { ProblemView } from "./ProblemView";
import styles from "./Onboarding.module.css";

export function AgentStep(): ReactNode {
  const agent = useAgentView();
  const [client, setClient] = useState<string>("");
  const [minted, setMinted] = useState<AgentToken | null>(null);
  const [minting, setMinting] = useState(false);
  const [problem, setProblem] = useState<SetupProblem | null>(null);
  const [promptOpen, setPromptOpen] = useState(false);
  const [prompt, setPrompt] = useState<string | null>(null);
  const [promptBusy, setPromptBusy] = useState(false);

  const clients = agent.data?.clients ?? [];
  const current: AgentClient | undefined = useMemo(
    () => clients.find((c) => c.id === client) ?? clients[0],
    [clients, client],
  );

  const snippet = useMemo(() => {
    if (!current) return "";
    const mintedClient = minted?.clients.find((c) => c.id === current.id);
    return mintedClient?.snippet ?? current.snippet;
  }, [current, minted]);

  const mint = async (): Promise<void> => {
    if (!current) return;
    setMinting(true);
    setProblem(null);
    try {
      setMinted(await mintAgentToken(current.label, current.id));
    } catch (e) {
      setProblem(setupProblem(e));
    } finally {
      setMinting(false);
    }
  };

  const openPrompt = async (): Promise<void> => {
    setPromptOpen(true);
    if (prompt) return;
    setPromptBusy(true);
    try {
      const r = await getSetupPrompt();
      setPrompt(r.markdown);
    } catch (e) {
      setPrompt(`Could not load the setup prompt: ${setupProblem(e).message}`);
    } finally {
      setPromptBusy(false);
    }
  };

  return (
    <div className={styles.step}>
      <StepHeader
        eyebrow="Connect your agent"
        title="Point your AI at your hub"
        lede="Throughline is built to be searched by your agents. Connect one over MCP and it can answer from everything you've gathered — your messages, your calendar, your places."
      />

      <div className={styles.body}>
        {agent.isLoading ? (
          <div className={styles.loadingStack}>
            <Skeleton height={56} />
            <Skeleton height={200} />
          </div>
        ) : agent.error ? (
          <ErrorState error={agent.error} title="Could not load agent details" onRetry={() => void agent.refetch()} />
        ) : agent.data ? (
          <>
            <div className={styles.agentUrl}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className={styles.agentUrlLabel}>MCP endpoint</div>
                <div className={styles.agentUrlValue}>{agent.data.mcp_url}</div>
              </div>
              <IconButton label="Copy MCP URL" onClick={() => void navigator.clipboard?.writeText(agent.data!.mcp_url)}>
                <Copy size={16} />
              </IconButton>
            </div>

            {clients.length > 0 && current && (
              <div>
                <Tabs
                  items={clients.map((c) => ({ value: c.id, label: c.label }))}
                  value={current.id}
                  onChange={setClient}
                />
                <div style={{ marginTop: "var(--s-4)" }}>
                  {current.steps.length > 0 && (
                    <ol className={styles.setupSteps} style={{ marginBottom: "var(--s-4)" }}>
                      {current.steps.map((s, i) => (
                        <li key={i} className={styles.setupStep}><span>{s}</span></li>
                      ))}
                    </ol>
                  )}
                  <CodeBlock code={snippet} lang={current.kind} wrap />
                  <div className={styles.cardActions} style={{ marginTop: "var(--s-3)" }}>
                    {minted ? (
                      <span className={styles.srcConnected}><Check size={14} /> Token created and dropped into the snippet above</span>
                    ) : (
                      <span className={styles.footerHint}>The snippet uses a <code>TOKEN</code> placeholder until you create one.</span>
                    )}
                    <span className={styles.footerSpacer} />
                    <Button variant="default" icon={<KeyRound size={15} />} onClick={() => void mint()} disabled={minting}>
                      {minting ? "Creating…" : "Create a token for this agent"}
                    </Button>
                  </div>
                  {minted && (
                    <div className={styles.tokenWarn}>
                      <TriangleAlert size={14} aria-hidden />
                      <span>Copy it now — this token is shown once and can't be retrieved again. Revoke it any time on the box.</span>
                    </div>
                  )}
                  {problem && <div style={{ marginTop: "var(--s-3)" }}><ProblemView problem={problem} /></div>}
                </div>
              </div>
            )}

            <Callout tone="info" icon={Wand2}>
              <strong>Rather let an agent do it?</strong> Copy a setup prompt and paste it into any agent — it'll walk you through the rest.
              <div style={{ marginTop: "var(--s-3)" }}>
                <Button size="sm" variant="default" icon={<Wand2 size={14} />} onClick={() => void openPrompt()}>
                  Set up with an agent instead
                </Button>
              </div>
            </Callout>
          </>
        ) : null}
      </div>

      <Dialog open={promptOpen} onClose={() => setPromptOpen(false)} title="Setup prompt" description="Paste this into any agent to be walked through setup." size="lg">
        {promptBusy ? (
          <Skeleton height={240} />
        ) : (
          <>
            <CodeBlock code={prompt ?? ""} wrap />
            <div className={styles.cardActions} style={{ marginTop: "var(--s-3)" }}>
              <span className={styles.footerSpacer} />
              <Button variant="primary" icon={<Copy size={15} />} onClick={() => prompt && void navigator.clipboard?.writeText(prompt)}>
                Copy prompt
              </Button>
            </div>
          </>
        )}
      </Dialog>

      <WizardFooter skipLabel="Skip" />
    </div>
  );
}
