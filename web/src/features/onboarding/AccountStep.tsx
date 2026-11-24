import { useMemo, useState, type FormEvent, type ReactNode } from "react";
import { UserCircle2, Check, ShieldCheck, Copy, ArrowUpRight, Loader2, Trash2, Plus } from "lucide-react";
import { KeyValue, Badge, Button, Field, Input, Select, Switch, Skeleton } from "@/ui";
import { useAuth } from "@/store/auth";
import { useBootstrap } from "@/api";
import { takeProviderFailure } from "@/api/auth";
import {
  useSetupAuth,
  useSaveAuthProvider,
  useRemoveAuthProvider,
  useUpdateAuthMethods,
  useStartAuthLink,
  setupProblem,
  type AuthProviderConfig,
  type SetupProblem,
} from "@/api/setup";
import { StepHeader, WizardFooter, Callout } from "./parts";
import { ProblemView } from "./ProblemView";
import styles from "./Onboarding.module.css";

/**
 * In-app account step. Shows your signed-in owner account and lets you
 * configure / link Authentik or any OpenID Connect provider right from
 * onboarding without editing env files by hand.
 */
export function AccountStep(): ReactNode {
  const account = useAuth((s) => s.account);
  const { data: boot } = useBootstrap();
  const authQ = useSetupAuth();
  const saveProvider = useSaveAuthProvider();
  const removeProvider = useRemoveAuthProvider();
  const updateMethods = useUpdateAuthMethods();
  const startLink = useStartAuthLink();

  const authView = authQ.data;
  const currentAcct = authView?.current_account;
  const name = currentAcct?.display_name ?? account?.display_name ?? currentAcct?.handle ?? account?.handle ?? boot?.token.name ?? "you";
  const handle = currentAcct?.handle ?? account?.handle;

  const linkedJustNow = useMemo(() => new URLSearchParams(window.location.search).get("linked"), []);
  const initialFailure = useMemo(() => {
    const f = takeProviderFailure();
    if (!f) return null;
    return {
      title: `Couldn't link ${f.provider}`,
      message: f.code === "already_linked"
        ? "That external identity is already linked to another Throughline account."
        : `Sign-in provider returned error: ${f.code}.`,
      hint: "Verify the Redirect URI and provider settings, then try again.",
    } satisfies SetupProblem;
  }, []);

  const [editing, setEditing] = useState(false);
  const [kind, setKind] = useState<"authentik" | "oidc">("authentik");
  const [providerId, setProviderId] = useState("authentik");
  const [label, setLabel] = useState("Authentik");
  const [issuer, setIssuer] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [allowedGroups, setAllowedGroups] = useState("");
  const [linkByEmail, setLinkByEmail] = useState(true);
  const [copiedUri, setCopiedUri] = useState(false);
  const [problem, setProblem] = useState<SetupProblem | null>(initialFailure);
  const [savedNotice, setSavedNotice] = useState<string | null>(null);

  const providers = authView?.providers ?? [];
  const linkedByProvider = new Map((currentAcct?.links ?? []).map((l) => [l.provider, l]));
  const redirectUri =
    authView?.default_redirect_uri
      ? authView.default_redirect_uri.replace(/\/authentik\/return$/, `/${providerId || "authentik"}/return`)
      : `${window.location.origin}/v1/auth/oidc/${providerId || "authentik"}/return`;

  function openNew(preset: "authentik" | "oidc" = "authentik"): void {
    setKind(preset);
    setProviderId(preset);
    setLabel(preset === "authentik" ? "Authentik" : "Single sign-on");
    setIssuer("");
    setClientId("");
    setClientSecret("");
    setAllowedGroups("");
    setLinkByEmail(true);
    setProblem(null);
    setSavedNotice(null);
    setEditing(true);
  }

  function openEdit(p: AuthProviderConfig): void {
    setKind(p.kind === "authentik" ? "authentik" : "oidc");
    setProviderId(p.id);
    setLabel(p.label);
    setIssuer(p.issuer);
    setClientId(p.client_id);
    setClientSecret(p.client_secret_hint ?? "");
    setAllowedGroups(p.allowed_groups.join(", "));
    setLinkByEmail(p.link_by_email);
    setProblem(null);
    setSavedNotice(null);
    setEditing(true);
  }

  function copyRedirect(uri: string): void {
    void navigator.clipboard?.writeText(uri).then(() => {
      setCopiedUri(true);
      setTimeout(() => setCopiedUri(false), 1800);
    });
  }

  function submitProvider(e: FormEvent): void {
    e.preventDefault();
    setProblem(null);
    setSavedNotice(null);
    saveProvider.mutate(
      {
        id: providerId.trim() || kind,
        kind,
        label: label.trim() || (kind === "authentik" ? "Authentik" : "Single sign-on"),
        issuer: issuer.trim(),
        client_id: clientId.trim(),
        client_secret: clientSecret.trim() || undefined,
        allowed_groups: allowedGroups.trim(),
        link_by_email: linkByEmail,
        verify: true,
      },
      {
        onSuccess: () => {
          setEditing(false);
          setSavedNotice(`${label.trim() || "SSO"} verified and saved.`);
        },
        onError: (err) => setProblem(setupProblem(err)),
      },
    );
  }

  function beginLink(pid: string): void {
    setProblem(null);
    startLink.mutate(
      { providerId: pid, returnTo: `/ui/welcome/account?linked=${encodeURIComponent(pid)}` },
      {
        onSuccess: (res) => window.location.assign(res.authorize_url),
        onError: (err) => setProblem(setupProblem(err)),
      },
    );
  }

  function deleteProvider(pid: string): void {
    setProblem(null);
    setSavedNotice(null);
    removeProvider.mutate(pid, {
      onError: (err) => setProblem(setupProblem(err)),
    });
  }

  function togglePassword(enabled: boolean): void {
    setProblem(null);
    updateMethods.mutate(
      { password: enabled },
      { onError: (err) => setProblem(setupProblem(err)) },
    );
  }

  const hasLinkedExternal = providers.some((p) => linkedByProvider.has(p.id));

  return (
    <div className={styles.step}>
      <StepHeader
        eyebrow="Your owner account"
        title="You're signed in"
        lede="The owner account is the key to your hub. Connect Authentik or another OpenID Connect provider here if you want single sign-on."
      />

      <div className={styles.body}>
        <div className={styles.importOverall}>
          <div className={styles.jobRowTop}>
            <span className={styles.roleIcon} style={{ color: "var(--ok)" }}>
              <UserCircle2 size={18} />
            </span>
            <div style={{ flex: 1 }}>
              <div className={styles.setupCardName}>{name}</div>
              {handle && <div className={styles.setupCardSummary}>@{handle}{currentAcct?.email ? ` · ${currentAcct.email}` : ""}</div>}
            </div>
            <Badge tone="ok">
              <Check size={11} /> owner
            </Badge>
          </div>
          {boot && (
            <KeyValue
              className={styles.confidence}
              rows={[
                { key: "Kind", value: <Badge tone="neutral">{boot.token.kind}</Badge> },
                { key: "Scope", value: boot.token.unrestricted ? <Badge tone="ok">unrestricted</Badge> : <Badge tone="warn">restricted</Badge> },
                ...(currentAcct?.links?.length
                  ? [
                      {
                        key: "SSO linked",
                        value: (
                          <span style={{ display: "inline-flex", gap: "var(--s-1)", flexWrap: "wrap" }}>
                            {currentAcct.links.map((l) => (
                              <Badge key={`${l.provider}:${l.subject}`} tone="ok">
                                <Check size={11} /> {l.provider}
                              </Badge>
                            ))}
                          </span>
                        ),
                      },
                    ]
                  : []),
              ]}
            />
          )}
        </div>

        {linkedJustNow && (
          <Callout tone="info">
            Linked <strong>{linkedJustNow}</strong> to your owner account. You can now sign in directly with {linkedJustNow}.
          </Callout>
        )}
        {savedNotice && <Callout tone="info">{savedNotice}</Callout>}
        {problem && <ProblemView problem={problem} />}

        <div className={styles.repoBox}>
          <div className={styles.repoBoxHead}>
            <ShieldCheck size={15} /> Single sign-on (Authentik / OpenID Connect)
          </div>
          <p className={styles.repoBoxNote}>
            Use your existing Authentik or any OIDC identity provider so you don&apos;t have to manage a separate password for Throughline.
          </p>

          {authQ.isLoading ? (
            <Skeleton height={44} />
          ) : (
            <>
              {providers.length > 0 && (
                <div className={styles.detectList} style={{ marginBottom: "var(--s-3)" }}>
                  {providers.map((p) => {
                    const link = linkedByProvider.get(p.id);
                    return (
                      <div key={p.id} className={styles.detectItem} style={{ flexWrap: "wrap", gap: "var(--s-2)" }}>
                        <ShieldCheck size={15} style={{ color: link ? "var(--ok)" : "var(--brass)" }} />
                        <div style={{ flex: 1, minWidth: 160 }}>
                          <div>
                            <strong>{p.label}</strong>{" "}
                            <span style={{ color: "var(--ink-3)", fontSize: "var(--t-xs)" }}>({p.id})</span>
                          </div>
                          <div style={{ color: "var(--ink-2)", fontSize: "var(--t-xs)", wordBreak: "break-all" }}>
                            {p.issuer}
                          </div>
                        </div>
                        {link ? (
                          <Badge tone="ok">
                            <Check size={11} /> linked{link.username ? ` as ${link.username}` : ""}
                          </Badge>
                        ) : currentAcct ? (
                          <Button
                            type="button"
                            size="sm"
                            variant="primary"
                            icon={startLink.isPending ? <Loader2 size={13} className={styles.spin} /> : <ArrowUpRight size={13} />}
                            disabled={startLink.isPending}
                            onClick={() => beginLink(p.id)}
                          >
                            Link my account
                          </Button>
                        ) : (
                          <Badge tone="neutral">configured</Badge>
                        )}
                        {!p.locked && (
                          <>
                            <Button type="button" size="sm" variant="ghost" onClick={() => openEdit(p)}>
                              Edit
                            </Button>
                            <Button
                              type="button"
                              size="sm"
                              variant="ghost"
                              icon={<Trash2 size={13} />}
                              disabled={removeProvider.isPending}
                              onClick={() => deleteProvider(p.id)}
                              aria-label={`Remove ${p.label}`}
                            >
                              Remove
                            </Button>
                          </>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}

              {editing ? (
                <form className={styles.repoForm} onSubmit={submitProvider} noValidate>
                  <div style={{ fontSize: "var(--t-xs)", color: "var(--ink-2)" }}>
                    In Authentik, create a <strong>Confidential</strong> OAuth2/OpenID Provider with scopes{" "}
                    <code>openid email profile</code> and Redirect URI:
                    <div style={{ display: "flex", alignItems: "center", gap: "var(--s-2)", marginTop: "var(--s-1)", flexWrap: "wrap" }}>
                      <code style={{ wordBreak: "break-all" }}>{redirectUri}</code>
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        icon={copiedUri ? <Check size={13} /> : <Copy size={13} />}
                        onClick={() => copyRedirect(redirectUri)}
                      >
                        {copiedUri ? "Copied" : "Copy"}
                      </Button>
                    </div>
                  </div>

                  <Field label="Provider type" htmlFor="step-oidc-kind">
                    <Select
                      id="step-oidc-kind"
                      value={kind}
                      onChange={(e) => {
                        const next = e.target.value as "authentik" | "oidc";
                        setKind(next);
                        if (providerId === "authentik" || providerId === "oidc") setProviderId(next);
                        if (label === "Authentik" || label === "Single sign-on") {
                          setLabel(next === "authentik" ? "Authentik" : "Single sign-on");
                        }
                      }}
                    >
                      <option value="authentik">Authentik</option>
                      <option value="oidc">Generic OpenID Connect</option>
                    </Select>
                  </Field>
                  <Field label="Issuer URL" htmlFor="step-oidc-issuer" hint="OpenID Configuration Issuer URL (e.g. https://auth.example.com/application/o/throughline/)">
                    <Input
                      id="step-oidc-issuer"
                      mono
                      spellCheck={false}
                      placeholder="https://auth.example.com/application/o/throughline/"
                      value={issuer}
                      onChange={(e) => setIssuer(e.target.value)}
                    />
                  </Field>
                  <Field label="Client ID" htmlFor="step-oidc-client-id">
                    <Input id="step-oidc-client-id" mono spellCheck={false} value={clientId} onChange={(e) => setClientId(e.target.value)} />
                  </Field>
                  <Field label="Client Secret" htmlFor="step-oidc-client-secret" hint="Leave unchanged to keep the saved secret; accepts infisical:SECRET_NAME">
                    <Input id="step-oidc-client-secret" type="password" autoComplete="off" value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} />
                  </Field>
                  <Field label="Allowed groups (optional)" htmlFor="step-oidc-groups" hint="Comma-separated group names">
                    <Input id="step-oidc-groups" spellCheck={false} placeholder="throughline-users" value={allowedGroups} onChange={(e) => setAllowedGroups(e.target.value)} />
                  </Field>
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "var(--s-3)" }}>
                    <span style={{ fontSize: "var(--t-sm)" }}>Auto-link accounts with matching verified email</span>
                    <Switch checked={linkByEmail} onChange={setLinkByEmail} aria-label="Auto-link by email" />
                  </div>
                  <div style={{ display: "flex", gap: "var(--s-2)", justifyContent: "flex-end" }}>
                    <Button type="button" variant="ghost" onClick={() => setEditing(false)} disabled={saveProvider.isPending}>
                      Cancel
                    </Button>
                    <Button
                      type="submit"
                      variant="primary"
                      icon={saveProvider.isPending ? <Loader2 size={14} className={styles.spin} /> : <Check size={14} />}
                      disabled={saveProvider.isPending || !issuer.trim() || !clientId.trim()}
                    >
                      {saveProvider.isPending ? "Testing discovery…" : "Test & save"}
                    </Button>
                  </div>
                </form>
              ) : authView?.can_configure_oidc ? (
                <div style={{ display: "flex", gap: "var(--s-2)", flexWrap: "wrap" }}>
                  <Button type="button" variant={providers.length === 0 ? "primary" : "ghost"} icon={<Plus size={14} />} onClick={() => openNew("authentik")}>
                    {providers.length === 0 ? "Connect Authentik / SSO" : "Add another OIDC provider"}
                  </Button>
                </div>
              ) : (
                <p style={{ fontSize: "var(--t-xs)", color: "var(--ink-2)", margin: 0 }}>
                  Sign-in providers are managed by server environment variables (<code>THROUGHLINE_AUTH_PROVIDERS</code>).
                </p>
              )}

              {hasLinkedExternal && !authView?.password_locked && (
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: "var(--s-3)",
                    marginTop: "var(--s-3)",
                    paddingTop: "var(--s-3)",
                    borderTop: "1px solid var(--line)",
                  }}
                >
                  <div>
                    <div style={{ fontSize: "var(--t-sm)", fontWeight: 500 }}>Allow local password sign-in</div>
                    <div style={{ fontSize: "var(--t-xs)", color: "var(--ink-2)" }}>
                      Turn off to require Authentik / SSO on the sign-in screen.
                    </div>
                  </div>
                  <Switch
                    checked={Boolean(authView?.password_enabled)}
                    disabled={updateMethods.isPending}
                    onChange={togglePassword}
                    aria-label="Allow local password sign-in"
                  />
                </div>
              )}
            </>
          )}
        </div>
      </div>

      <WizardFooter hideSkip />
    </div>
  );
}
