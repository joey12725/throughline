/*
 * Sign-in calls (docs/web/auth.md). These run before anyone is signed in, so
 * they never trigger the client's sign-out-on-401.
 */
import { request } from "./client";
import type { Account, SessionInfo } from "@/store/auth";

export interface SignInProvider {
  id: string;
  /** preset family: "authentik", "oidc", … */
  kind: string;
  label: string;
  /** path that starts the redirect flow */
  start: string;
}

export interface AuthConfig {
  methods: { password: boolean; token: boolean };
  providers: SignInProvider[];
  /** false until the first account is created on the box */
  accounts: boolean;
}

/** What a server without sign-in support (or unreachable) is treated as. */
export const TOKEN_ONLY: AuthConfig = {
  methods: { password: false, token: true },
  providers: [],
  accounts: false,
};

export async function getAuthConfig(signal?: AbortSignal): Promise<AuthConfig> {
  try {
    return await request<AuthConfig>("/auth/config", { signal, noAuthRedirect: true });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    return TOKEN_ONLY;
  }
}

export interface SignedIn {
  account: Account;
  session: SessionInfo;
}

export function passwordSignIn(username: string, password: string, remember: boolean): Promise<SignedIn> {
  return request<SignedIn>("/auth/login", {
    body: { username, password, remember },
    noAuthRedirect: true,
  });
}

/** Full-page navigation to the provider; the server redirects back. */
export function providerStartUrl(provider: SignInProvider, returnTo: string, remember: boolean): string {
  const q = new URLSearchParams({ return_to: returnTo, remember: remember ? "true" : "false" });
  return `${provider.start}?${q.toString()}`;
}

/** An error handed back from a provider sign-in in the URL fragment. */
export interface ProviderFailure {
  code: string;
  provider: string | null;
  subject: string | null;
}

/** Read #auth_error=…&provider=…&subject=… and scrub it from history. */
export function takeProviderFailure(): ProviderFailure | null {
  const hash = window.location.hash.replace(/^#/, "");
  if (!hash.includes("auth_error=")) return null;
  const params = new URLSearchParams(hash);
  const code = params.get("auth_error");
  history.replaceState(null, "", window.location.pathname + window.location.search);
  if (!code) return null;
  return { code, provider: params.get("provider"), subject: params.get("subject") };
}
