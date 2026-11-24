/*
 * The pre-sign-in gate. When no owner account exists, the app shows this instead
 * of the plain sign-in card: the wizard chrome with the account step live. It is
 * rendered OUTSIDE the router (no WizardProvider), so it is fully self-contained.
 * Supports claiming the first owner account either with an existing Authentik /
 * OpenID Connect provider (configured in env or right here in the browser) or
 * with a local username and password.
 */
import { useMemo, useState, type FormEvent, type ReactNode } from "react";
import { ArrowRight, ArrowUpRight, Check, Copy, KeyRound, Loader2, ShieldCheck } from "lucide-react";
import { Field, Input, Button, Select } from "@/ui";
import { takeProviderFailure, type ProviderFailure } from "@/api/auth";
import {
  createFirstAccount,
  startFirstAccountOidc,
  setupProblem,
  type PublicSetup,
  type SetupProblem,
} from "@/api/setup";
import type { Account, SessionInfo } from "@/store/auth";
import { useAuth } from "@/store/auth";
import { WizardFrame } from "./WizardFrame";
import { StepHeader, Callout } from "./parts";
import { ProblemView } from "./ProblemView";
import styles from "./Onboarding.module.css";

function passwordStrength(pw: string): { level: "weak" | "fair" | "good"; bars: number; label: string } {
  let score = 0;
  if (pw.length >= 10) score++;
  if (pw.length >= 14) score++;
  if (/[^a-zA-Z0-9]/.test(pw) || (/[a-z]/.test(pw) && /[A-Z]/.test(pw) && /\d/.test(pw))) score++;
  if (score <= 1) return { level: "weak", bars: 1, label: "Too short — use at least 10 characters" };
  if (score === 2) return { level: "fair", bars: 2, label: "Fair — longer is stronger" };
  return { level: "good", bars: 3, label: "Strong" };
}

function oidcFailureMessage(failure: ProviderFailure): SetupProblem {
  const map: Record<string, { title: string; message: string; hint?: string }> = {
    denied: {
      title: "Sign-in was cancelled",
      message: `The sign-in with ${failure.provider} didn't finish.`,
      hint: "Try again or check that your user is assigned to the Throughline application in Authentik.",
    },
    expired: {
      title: "Sign-in session expired",
      message: "The browser state didn't match or took too long.",
      hint: "Start the sign-in flow again from this page.",
    },
    not_permitted: {
      title: "Not in an allowed group",
      message: `Your ${failure.provider} account isn't a member of the allowed groups configured for Throughline.`,
      hint: "Add your user to the group in Authentik or clear the Allowed groups field.",
    },
    already_claimed: {
      title: "An owner account already exists",
      message: "This install already has an owner account.",
      hint: "Refresh the page to sign in.",
    },
    unreachable: {
      title: "Couldn't reach the sign-in provider",
      message: `The Throughline server couldn't complete the token exchange with ${failure.provider}.`,
      hint: "Confirm the Issuer URL is reachable from the Throughline server.",
    },
  };
  const info = map[failure.code] ?? {
    title: "External sign-in failed",
    message: `${failure.provider} returned an error (${failure.code}).`,
    hint: "Check the redirect URI and client credentials in your provider, then try again.",
  };
  return info;
}

export function AccountGate({ publicSetup }: { publicSetup: PublicSetup }): ReactNode {
  const checkSession = useAuth((s) => s.checkSession);
  const setSession = useAuth((s) => s.setSession);

  const existingProviders = publicSetup.providers ?? [];
  const canConfigureOidc = publicSetup.can_configure_oidc !== false;
  const hasOidcOption = existingProviders.length > 0 || canConfigureOidc;

  const prefilledCode = useMemo(() => new URLSearchParams(window.location.search).get("setup") ?? "", []);
  const initialFailure = useMemo(() => {
    const f = takeProviderFailure();
    return f ? oidcFailureMessage(f) : null;
  }, []);

  const [authMode, setAuthMode] = useState<"password" | "oidc">(() =>
    existingProviders.length > 0 || initialFailure ? "oidc" : "password",
  );
  const [showCustomForm, setShowCustomForm] = useState(() => existingProviders.length === 0);

  // Local password form state
  const [handle, setHandle] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");

  // Authentik / OIDC form state
  const [kind, setKind] = useState<"authentik" | "oidc">("authentik");
  const [issuer, setIssuer] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [allowedGroups, setAllowedGroups] = useState("");
  const [oidcHandle, setOidcHandle] = useState("");
  const [copiedRedirect, setCopiedRedirect] = useState(false);

  const [code, setCode] = useState(prefilledCode);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<SetupProblem | null>(initialFailure);

  const strength = passwordStrength(password);
  const showCodeField = publicSetup.code_required && !prefilledCode;
  const mismatch = confirm.length > 0 && confirm !== password;
  const passwordValid =
    handle.trim().length > 0 &&
    password.length >= 8 &&
    confirm === password &&
    (!publicSetup.code_required || code.trim().length > 0);
  const oidcConfigureValid =
    issuer.trim().length > 0 &&
    clientId.trim().length > 0 &&
    (!publicSetup.code_required || code.trim().length > 0);

  const redirectUri =
    publicSetup.redirect_uri || `${window.location.origin}/v1/auth/oidc/${kind === "authentik" ? "authentik" : "oidc"}/return`;

  function copyRedirect(): void {
    void navigator.clipboard?.writeText(redirectUri).then(() => {
      setCopiedRedirect(true);
      setTimeout(() => setCopiedRedirect(false), 1800);
    });
  }

  async function submitPassword(e: FormEvent): Promise<void> {
    e.preventDefault();
    if (!passwordValid || busy) return;
    setBusy(true);
    setProblem(null);
    try {
      const res = (await createFirstAccount({
        code: code.trim(),
        handle: handle.trim(),
        display_name: displayName.trim() || undefined,
        email: email.trim() || undefined,
        password,
      })) as { account?: Account; session?: SessionInfo };
      if (res.account && res.session) setSession(res.account, res.session);
      await checkSession();
    } catch (err) {
      setProblem(setupProblem(err));
      setBusy(false);
    }
  }

  async function startExistingProvider(providerId: string): Promise<void> {
    if (busy) return;
    if (publicSetup.code_required && !code.trim()) {
      setProblem({
        title: "Enter the setup code",
        message: "The one-time setup code is required to claim the owner account.",
        hint: publicSetup.code_hint,
      });
      return;
    }
    setBusy(true);
    setProblem(null);
    try {
      const res = await startFirstAccountOidc({
        code: code.trim(),
        provider_id: providerId,
        handle: oidcHandle.trim() || undefined,
        remember: true,
      });
      window.location.assign(res.authorize_url);
    } catch (err) {
      setProblem(setupProblem(err));
      setBusy(false);
    }
  }

  async function submitOidcConfigure(e: FormEvent): Promise<void> {
    e.preventDefault();
    if (!oidcConfigureValid || busy) return;
    setBusy(true);
    setProblem(null);
    try {
      const providerId = kind === "authentik" ? "authentik" : "oidc";
      const res = await startFirstAccountOidc({
        code: code.trim(),
        provider_id: providerId,
        kind,
        label: kind === "authentik" ? "Authentik" : "Single sign-on",
        issuer: issuer.trim(),
        client_id: clientId.trim(),
        client_secret: clientSecret.trim() || undefined,
        allowed_groups: allowedGroups.trim() || undefined,
        link_by_email: true,
        handle: oidcHandle.trim() || undefined,
        remember: true,
      });
      window.location.assign(res.authorize_url);
    } catch (err) {
      setProblem(setupProblem(err));
      setBusy(false);
    }
  }

  return (
    <WizardFrame current="account" statusOf={() => "todo"} progress={0}>
      <div className={styles.step}>
        <StepHeader
          eyebrow="Your owner account"
          title="Create your owner account"
          lede={`This is the key to ${publicSetup.instance_name || "your hub"} — the one account that can configure everything. Sign in with Authentik / SSO or create a local password.`}
        />

        <div className={styles.body}>
          {hasOidcOption && (
            <div className={styles.cards} role="radiogroup" aria-label="Sign-in method">
              <button
                type="button"
                role="radio"
                aria-checked={authMode === "oidc"}
                className={styles.choice}
                data-selected={authMode === "oidc"}
                onClick={() => {
                  setAuthMode("oidc");
                  setProblem(null);
                }}
              >
                <span className={styles.choiceIcon}>
                  <ShieldCheck size={20} />
                </span>
                <span className={styles.choiceText}>
                  <span className={styles.choiceTitle}>Use Authentik / SSO</span>
                  <span className={styles.choiceDesc}>
                    {existingProviders.length > 0
                      ? `Claim this hub with ${existingProviders.map((p) => p.label).join(" or ")} — no separate password needed.`
                      : "Connect your existing Authentik or OpenID Connect provider and sign straight in."}
                  </span>
                </span>
                {authMode === "oidc" && <Check size={18} className={styles.choiceMark} aria-hidden />}
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={authMode === "password"}
                className={styles.choice}
                data-selected={authMode === "password"}
                onClick={() => {
                  setAuthMode("password");
                  setProblem(null);
                }}
              >
                <span className={styles.choiceIcon}>
                  <KeyRound size={20} />
                </span>
                <span className={styles.choiceText}>
                  <span className={styles.choiceTitle}>Username &amp; password</span>
                  <span className={styles.choiceDesc}>
                    Create a local owner account on this box (you can still link Authentik right after).
                  </span>
                </span>
                {authMode === "password" && <Check size={18} className={styles.choiceMark} aria-hidden />}
              </button>
            </div>
          )}

          {authMode === "oidc" ? (
            <div className={styles.accountForm}>
              {existingProviders.length > 0 && !showCustomForm ? (
                <>
                  {showCodeField && (
                    <Field label="Setup code" htmlFor="oidc-code" hint={publicSetup.code_hint || "The code printed by `throughline serve`"}>
                      <Input id="oidc-code" mono autoComplete="off" spellCheck={false} placeholder="setup code" value={code} onChange={(e) => setCode(e.target.value)} />
                    </Field>
                  )}
                  <Field label="Handle (optional)" htmlFor="oidc-handle" hint="Defaults to your Authentik username">
                    <Input id="oidc-handle" autoCapitalize="none" spellCheck={false} placeholder="you" value={oidcHandle} onChange={(e) => setOidcHandle(e.target.value)} />
                  </Field>
                  {problem && <ProblemView problem={problem} />}
                  <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--s-3)", alignItems: "center" }}>
                    {existingProviders.map((p) => (
                      <Button
                        key={p.id}
                        type="button"
                        variant="primary"
                        icon={busy ? <Loader2 size={15} className={styles.spin} /> : <ArrowUpRight size={15} />}
                        disabled={busy || (publicSetup.code_required && !code.trim())}
                        onClick={() => void startExistingProvider(p.id)}
                      >
                        {busy ? "Redirecting…" : `Continue with ${p.label}`}
                      </Button>
                    ))}
                    {canConfigureOidc && (
                      <Button type="button" variant="ghost" onClick={() => setShowCustomForm(true)} disabled={busy}>
                        Configure a different provider
                      </Button>
                    )}
                  </div>
                </>
              ) : (
                <form className={styles.accountForm} onSubmit={(e) => void submitOidcConfigure(e)} noValidate>
                  <Callout tone="info">
                    In Authentik, create a <strong>Confidential</strong> OAuth2/OpenID Provider with scopes{" "}
                    <code>openid email profile</code> and this Redirect URI:
                    <div style={{ display: "flex", alignItems: "center", gap: "var(--s-2)", marginTop: "var(--s-2)", flexWrap: "wrap" }}>
                      <code style={{ wordBreak: "break-all" }}>{redirectUri}</code>
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        icon={copiedRedirect ? <Check size={13} /> : <Copy size={13} />}
                        onClick={copyRedirect}
                      >
                        {copiedRedirect ? "Copied" : "Copy"}
                      </Button>
                    </div>
                  </Callout>

                  <Field label="Provider type" htmlFor="oidc-kind">
                    <Select id="oidc-kind" value={kind} onChange={(e) => setKind(e.target.value as "authentik" | "oidc")}>
                      <option value="authentik">Authentik</option>
                      <option value="oidc">Generic OpenID Connect (Keycloak, Authelia, Pocket ID, …)</option>
                    </Select>
                  </Field>
                  <Field
                    label="Issuer URL"
                    htmlFor="oidc-issuer"
                    hint="In Authentik: Provider → OpenID Configuration Issuer (ends with /application/o/<slug>/)"
                  >
                    <Input
                      id="oidc-issuer"
                      mono
                      autoFocus
                      spellCheck={false}
                      placeholder="https://auth.example.com/application/o/throughline/"
                      value={issuer}
                      onChange={(e) => setIssuer(e.target.value)}
                    />
                  </Field>
                  <Field label="Client ID" htmlFor="oidc-client-id">
                    <Input
                      id="oidc-client-id"
                      mono
                      spellCheck={false}
                      placeholder="OAuth2 Client ID"
                      value={clientId}
                      onChange={(e) => setClientId(e.target.value)}
                    />
                  </Field>
                  <Field
                    label="Client Secret"
                    htmlFor="oidc-client-secret"
                    hint="Recommended for Confidential clients; also accepts infisical:SECRET_NAME"
                  >
                    <Input
                      id="oidc-client-secret"
                      type="password"
                      autoComplete="off"
                      placeholder="OAuth2 Client Secret"
                      value={clientSecret}
                      onChange={(e) => setClientSecret(e.target.value)}
                    />
                  </Field>
                  <Field label="Allowed groups (optional)" htmlFor="oidc-groups" hint="Comma-separated group names; leave empty to allow any user assigned in Authentik">
                    <Input id="oidc-groups" spellCheck={false} placeholder="throughline-users" value={allowedGroups} onChange={(e) => setAllowedGroups(e.target.value)} />
                  </Field>
                  <Field label="Handle (optional)" htmlFor="oidc-custom-handle" hint="Defaults to your Authentik username">
                    <Input id="oidc-custom-handle" autoCapitalize="none" spellCheck={false} placeholder="you" value={oidcHandle} onChange={(e) => setOidcHandle(e.target.value)} />
                  </Field>
                  {showCodeField && (
                    <Field label="Setup code" htmlFor="oidc-setup-code" hint={publicSetup.code_hint || "The code printed by `throughline serve`"}>
                      <Input id="oidc-setup-code" mono autoComplete="off" spellCheck={false} placeholder="setup code" value={code} onChange={(e) => setCode(e.target.value)} />
                    </Field>
                  )}

                  {problem && <ProblemView problem={problem} />}

                  <footer className={styles.footer}>
                    {existingProviders.length > 0 && (
                      <Button type="button" variant="ghost" onClick={() => setShowCustomForm(false)} disabled={busy}>
                        Back to configured providers
                      </Button>
                    )}
                    <span className={styles.footerSpacer} />
                    <Button
                      type="submit"
                      variant="primary"
                      icon={busy ? <Loader2 size={15} className={styles.spin} /> : <ArrowUpRight size={15} />}
                      disabled={!oidcConfigureValid || busy}
                    >
                      {busy ? "Testing & redirecting…" : `Connect & sign in with ${kind === "authentik" ? "Authentik" : "SSO"}`}
                    </Button>
                  </footer>
                </form>
              )}
            </div>
          ) : (
            <form className={styles.accountForm} onSubmit={(e) => void submitPassword(e)} noValidate>
              <Field label="Handle" htmlFor="acct-handle" hint="Your username for signing in">
                <Input id="acct-handle" autoFocus autoComplete="username" autoCapitalize="none" spellCheck={false} placeholder="you" value={handle} onChange={(e) => setHandle(e.target.value)} />
              </Field>
              <Field label="Display name" htmlFor="acct-name" hint="Optional — how your name appears">
                <Input id="acct-name" autoComplete="name" placeholder="Your name" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
              </Field>
              <Field label="Email" htmlFor="acct-email" hint="Optional — lets Authentik / SSO link to this account automatically">
                <Input id="acct-email" type="email" autoComplete="email" placeholder="you@example.com" value={email} onChange={(e) => setEmail(e.target.value)} />
              </Field>
              <Field label="Password" htmlFor="acct-pass">
                <Input id="acct-pass" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
                {password.length > 0 && (
                  <>
                    <div className={styles.strength} aria-hidden>
                      {[0, 1, 2].map((i) => (
                        <span key={i} className={styles.strengthBar} data-on={i < strength.bars} data-level={strength.level} />
                      ))}
                    </div>
                    <div className={styles.strengthLabel}>{strength.label}</div>
                  </>
                )}
              </Field>
              <Field label="Confirm password" htmlFor="acct-confirm" error={mismatch ? "Passwords don't match" : undefined}>
                <Input id="acct-confirm" type="password" autoComplete="new-password" invalid={mismatch} value={confirm} onChange={(e) => setConfirm(e.target.value)} />
              </Field>
              {showCodeField && (
                <Field label="Setup code" htmlFor="acct-code" hint={publicSetup.code_hint || "The code printed by `throughline serve`"}>
                  <Input id="acct-code" mono autoComplete="off" spellCheck={false} placeholder="setup code" value={code} onChange={(e) => setCode(e.target.value)} />
                </Field>
              )}

              {problem && <ProblemView problem={problem} />}

              <footer className={styles.footer}>
                <span className={styles.footerSpacer} />
                <Button type="submit" variant="primary" icon={busy ? <Loader2 size={15} className={styles.spin} /> : <ArrowRight size={15} />} disabled={!passwordValid || busy}>
                  {busy ? "Creating…" : "Create account"}
                </Button>
              </footer>
            </form>
          )}
        </div>
      </div>
    </WizardFrame>
  );
}
