import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react";
import { ArrowLeft, ArrowRight, ArrowUpRight, KeyRound, LoaderCircle } from "lucide-react";
import { Button, Field, Input, Switch, CodeBlock, Skeleton } from "@/ui";
import { ApiError, request } from "@/api";
import {
  getAuthConfig,
  passwordSignIn,
  providerStartUrl,
  takeProviderFailure,
  type AuthConfig,
  type ProviderFailure,
  type SignInProvider,
} from "@/api/auth";
import { useAuth } from "@/store/auth";
import styles from "./SignInScreen.module.css";

/**
 * The sign-in screen (docs/web/auth.md). It offers whatever the server is
 * configured for, in this order: external providers (Authentik, any OIDC),
 * a username and password, and pasting an access token. Nothing here mints
 * credentials; accounts and tokens are made on the box.
 */
export function SignInScreen(): ReactNode {
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [view, setView] = useState<"methods" | "token">("methods");
  const [failure] = useState<ProviderFailure | null>(() => takeProviderFailure());
  const [remember, setRemember] = useState(false);
  const [leaving, setLeaving] = useState<string | null>(null);
  // A token handed in the URL fragment (#token=…) goes straight to the token view.
  const [handedToken, setHandedToken] = useState<string | null>(() => readFragmentToken());

  useEffect(() => {
    const ctl = new AbortController();
    getAuthConfig(ctl.signal).then(setConfig, () => undefined);
    return () => ctl.abort();
  }, []);

  const onlyToken = config !== null && !config.methods.password && config.providers.length === 0;
  const showToken = view === "token" || handedToken !== null || onlyToken;

  function goToProvider(p: SignInProvider): void {
    setLeaving(p.id);
    const back = window.location.pathname.startsWith("/ui") ? window.location.pathname + window.location.search : "/ui/";
    window.location.assign(providerStartUrl(p, back, remember));
  }

  return (
    <div className={styles.screen}>
      <div className={styles.vignette} aria-hidden />
      <main className={styles.card} aria-labelledby="signin-title">
        <span className={styles.thread} aria-hidden />
        <header className={styles.brand}>
          <img src="/ui/icon.svg" alt="" width={44} height={44} />
          <div>
            <h1 id="signin-title" className={styles.title}>Throughline</h1>
            <p className={styles.tagline}>An observatory for your archive.</p>
          </div>
        </header>

        {failure && <FailureNote failure={failure} providers={config?.providers ?? []} />}

        {config === null ? (
          <div className={styles.loading} aria-busy="true" aria-label="Loading sign-in options">
            <Skeleton height={36} />
            <Skeleton height={36} />
          </div>
        ) : showToken ? (
          <TokenForm
            initial={handedToken}
            onBack={
              onlyToken
                ? undefined
                : () => {
                    setHandedToken(null);
                    setView("methods");
                  }
            }
          />
        ) : (
          <div className={styles.methods}>
            {config.providers.length > 0 && (
              <div className={styles.providers} role="group" aria-label="Sign in with">
                {config.providers.map((p) => (
                  <button
                    key={p.id}
                    type="button"
                    className={styles.provider}
                    onClick={() => goToProvider(p)}
                    disabled={leaving !== null}
                  >
                    <ProviderMark provider={p} />
                    <span className={styles.providerLabel}>
                      {leaving === p.id ? `Opening ${p.label}…` : `Continue with ${p.label}`}
                    </span>
                    {leaving === p.id ? (
                      <LoaderCircle size={15} className={styles.spin} aria-hidden />
                    ) : (
                      <ArrowUpRight size={15} className={styles.providerArrow} aria-hidden />
                    )}
                  </button>
                ))}
              </div>
            )}

            {config.providers.length > 0 && config.methods.password && (
              <div className={styles.divider} role="separator">
                <span>or</span>
              </div>
            )}

            {config.methods.password && config.accounts && (
              <PasswordForm remember={remember} onRemember={setRemember} disabled={leaving !== null} />
            )}

            {config.methods.password && !config.accounts && <FirstAccount />}

            {!(config.methods.password && config.accounts) && config.providers.length > 0 && (
              <RememberRow remember={remember} onRemember={setRemember} />
            )}

            {config.methods.token && (
              <button type="button" className={styles.alt} onClick={() => setView("token")}>
                <KeyRound size={14} aria-hidden />
                Use an access token instead
              </button>
            )}
          </div>
        )}
      </main>
      <p className={styles.foot}>Your archive stays on your server. This page talks to nothing else.</p>
    </div>
  );
}

function readFragmentToken(): string | null {
  const m = /(?:^#|&)token=([^&]+)/.exec(window.location.hash);
  if (!m || !m[1]) return null;
  history.replaceState(null, "", window.location.pathname + window.location.search);
  return decodeURIComponent(m[1]);
}

/** A neutral monogram tile. A preset may get a bundled mark later. */
function ProviderMark({ provider }: { provider: SignInProvider }): ReactNode {
  const letter = (provider.label.trim()[0] ?? "?").toUpperCase();
  return (
    <span className={styles.mark} data-kind={provider.kind} aria-hidden>
      {letter}
    </span>
  );
}

function RememberRow({ remember, onRemember }: { remember: boolean; onRemember: (v: boolean) => void }): ReactNode {
  const id = useId();
  return (
    <div className={styles.remember}>
      <Switch id={id} checked={remember} onChange={onRemember} />
      <label htmlFor={id}>Stay signed in on this device</label>
    </div>
  );
}

function PasswordForm({
  remember,
  onRemember,
  disabled,
}: {
  remember: boolean;
  onRemember: (v: boolean) => void;
  disabled: boolean;
}): ReactNode {
  const setSession = useAuth((s) => s.setSession);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const userRef = useRef<HTMLInputElement | null>(null);
  const passRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => userRef.current?.focus(), []);

  async function submit(e: FormEvent): Promise<void> {
    e.preventDefault();
    if (!username.trim() || !password) return;
    setBusy(true);
    setError(null);
    try {
      const done = await passwordSignIn(username.trim(), password, remember);
      setSession(done.account, done.session);
    } catch (err) {
      setPassword("");
      setError(describeLoginError(err));
      passRef.current?.focus();
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className={styles.form} onSubmit={(e) => void submit(e)} noValidate>
      <Field label="Username" htmlFor="signin-user">
        <Input
          id="signin-user"
          ref={userRef}
          name="username"
          autoComplete="username"
          autoCapitalize="none"
          spellCheck={false}
          placeholder="handle or e-mail"
          value={username}
          invalid={Boolean(error)}
          onChange={(e) => { setUsername(e.target.value); setError(null); }}
        />
      </Field>
      <Field label="Password" htmlFor="signin-pass">
        <Input
          id="signin-pass"
          ref={passRef}
          name="password"
          type="password"
          autoComplete="current-password"
          value={password}
          invalid={Boolean(error)}
          onChange={(e) => { setPassword(e.target.value); setError(null); }}
        />
      </Field>
      <RememberRow remember={remember} onRemember={onRemember} />
      {error && (
        <p className={styles.inlineError} role="alert">
          {error}
        </p>
      )}
      <Button
        type="submit"
        variant="primary"
        block
        disabled={busy || disabled || !username.trim() || !password}
        icon={busy ? <LoaderCircle size={15} className={styles.spin} /> : <ArrowRight size={15} />}
      >
        {busy ? "Signing in…" : "Sign in"}
      </Button>
    </form>
  );
}

function describeLoginError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return "That username and password don't match an account.";
    if (err.status === 429) {
      const wait = (err.detail as { retry_after_s?: number } | null)?.retry_after_s;
      const minutes = wait ? Math.max(1, Math.ceil(wait / 60)) : null;
      return minutes
        ? `Too many attempts. Try again in ${minutes} minute${minutes === 1 ? "" : "s"}.`
        : "Too many attempts. Try again later.";
    }
    if (err.status === 0) return "Couldn't reach the server.";
    return err.message;
  }
  return "Sign-in failed.";
}

function FirstAccount(): ReactNode {
  return (
    <div className={styles.setup}>
      <div className={styles.setupHead}>No accounts yet</div>
      <p>Create the first one on the machine that runs Throughline, then sign in here:</p>
      <CodeBlock code="throughline lib account create <handle> --password" wrap />
    </div>
  );
}

const FAILURES: Record<string, (label: string) => string> = {
  denied: (p) => `Sign-in was cancelled at ${p}.`,
  expired: () => "That sign-in expired or was started in another tab. Try again.",
  unlinked: (p) => `${p} knows you, but no Throughline account is linked to that identity yet.`,
  not_permitted: (p) => `Your ${p} account isn't in a group allowed to use Throughline.`,
  disabled: () => "This account is disabled.",
  unreachable: (p) => `Throughline couldn't reach ${p}. Check that it is up and reachable from the server.`,
  provider: (p) => `${p} answered in a way Throughline couldn't verify. The server log has the details.`,
};

function FailureNote({ failure, providers }: { failure: ProviderFailure; providers: SignInProvider[] }): ReactNode {
  const label = providers.find((p) => p.id === failure.provider)?.label ?? failure.provider ?? "The provider";
  const text = (FAILURES[failure.code] ?? FAILURES.provider!)(label);
  return (
    <div className={styles.failure} role="alert">
      <p>{text}</p>
      {failure.code === "unlinked" && failure.subject && (
        <>
          <p className={styles.failureHint}>On the box, link it to your account:</p>
          <CodeBlock
            code={`throughline lib account link <handle> --provider ${failure.provider ?? "<id>"} --subject ${failure.subject}`}
           
            wrap
          />
        </>
      )}
    </div>
  );
}

function TokenForm({ initial, onBack }: { initial: string | null; onBack?: () => void }): ReactNode {
  const setToken = useAuth((s) => s.setToken);
  const [value, setValue] = useState(initial ?? "");
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (initial) void verify(initial, false);
    else inputRef.current?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function verify(token: string, rememberIt: boolean): Promise<void> {
    const trimmed = token.trim();
    if (!trimmed) return;
    setBusy(true);
    setError(null);
    try {
      // Probe a cheap, owner/agent-readable route with this token.
      await request("/status", {
        headers: { Authorization: `Bearer ${trimmed}` },
        noAuthRedirect: true,
      });
      setToken(trimmed, rememberIt);
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? "That token was not accepted." : describeLoginError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className={styles.form}
      onSubmit={(e) => {
        e.preventDefault();
        void verify(value, remember);
      }}
    >
      <Field label="Access token" htmlFor="token" hint="Kept in this browser only">
        <Input
          id="token"
          ref={inputRef}
          mono
          type="password"
          autoComplete="off"
          spellCheck={false}
          placeholder="throughline token…"
          value={value}
          invalid={Boolean(error)}
          onChange={(e) => { setValue(e.target.value); setError(null); }}
        />
      </Field>
      <RememberRow remember={remember} onRemember={setRemember} />
      {error && (
        <p className={styles.inlineError} role="alert">
          {error}
        </p>
      )}
      <Button type="submit" variant="primary" block disabled={busy || !value.trim()} icon={<ArrowRight size={15} />}>
        {busy ? "Checking…" : "Enter"}
      </Button>
      <div className={styles.help}>
        <p>Tokens are minted on the machine that runs Throughline, never in the browser:</p>
        <CodeBlock code="throughline token create --kind owner --name web" wrap />
      </div>
      {onBack && (
        <button type="button" className={styles.alt} onClick={onBack}>
          <ArrowLeft size={14} aria-hidden />
          Other ways to sign in
        </button>
      )}
    </form>
  );
}
