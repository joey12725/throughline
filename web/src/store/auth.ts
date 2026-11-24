/*
 * Auth store. There are two ways a browser is signed in:
 *
 *   - "session": the server set an HttpOnly session cookie after a password or
 *     provider sign-in (docs/web/auth.md). The page never sees the secret; it
 *     only knows the account it belongs to.
 *   - "token": an access token minted on the box (`throughline token create`)
 *     and pasted here. sessionStorage by default; "remember on this device"
 *     opts into localStorage. Sent as the Authorization bearer.
 *
 * Either way the server resolves the request to one principal token, so the
 * rest of the interface does not care which. The owner may also act as an
 * asker (X-Throughline-Asker).
 *
 * This module must not import the fetch client (the client imports it).
 */
import { create } from "zustand";

const SESSION_KEY = "tl.token";
const REMEMBER_KEY = "tl.token.remember";
const ASKER_KEY = "tl.asker";

export interface Account {
  id: number;
  handle: string;
  display_name: string | null;
  email: string | null;
}

export interface SessionInfo {
  /** "password" or the provider id that signed this browser in */
  method: string;
  persistent: boolean;
  created_at: string;
  expires_at: string;
}

export type AuthStatus = "checking" | "signedOut" | "signedIn";

function readStored(): { token: string | null; remember: boolean } {
  try {
    const remembered = localStorage.getItem(SESSION_KEY);
    if (remembered) return { token: remembered, remember: true };
  } catch {
    /* storage blocked */
  }
  try {
    const session = sessionStorage.getItem(SESSION_KEY);
    if (session) return { token: session, remember: localStorage.getItem(REMEMBER_KEY) === "1" };
  } catch {
    /* storage blocked */
  }
  return { token: null, remember: false };
}

function readAsker(): string | null {
  try {
    return sessionStorage.getItem(ASKER_KEY);
  } catch {
    return null;
  }
}

function clearStoredToken(): void {
  try {
    sessionStorage.removeItem(SESSION_KEY);
    localStorage.removeItem(SESSION_KEY);
    localStorage.removeItem(REMEMBER_KEY);
  } catch {
    /* ignore */
  }
}

interface AuthState {
  status: AuthStatus;
  mode: "token" | "session" | null;
  token: string | null;
  remember: boolean;
  account: Account | null;
  session: SessionInfo | null;
  /** owner-only "act as asker"; null = act as owner */
  actingAsker: string | null;
  setToken: (token: string, remember: boolean) => void;
  setSession: (account: Account, session: SessionInfo) => void;
  /** Ask the server whether this browser holds a live session cookie. */
  checkSession: () => Promise<void>;
  signOut: () => void;
  setActingAsker: (asker: string | null) => void;
}

const initial = readStored();

interface SessionAnswer {
  account: Account | null;
  session?: SessionInfo;
}

export const useAuth = create<AuthState>((set, get) => ({
  status: initial.token ? "signedIn" : "checking",
  mode: initial.token ? "token" : null,
  token: initial.token,
  remember: initial.remember,
  account: null,
  session: null,
  actingAsker: readAsker(),

  setToken: (token, remember) => {
    try {
      sessionStorage.setItem(SESSION_KEY, token);
      if (remember) {
        localStorage.setItem(SESSION_KEY, token);
        localStorage.setItem(REMEMBER_KEY, "1");
      } else {
        localStorage.removeItem(SESSION_KEY);
        localStorage.removeItem(REMEMBER_KEY);
      }
    } catch {
      /* storage blocked — token stays in memory for this tab */
    }
    set({ status: "signedIn", mode: "token", token, remember, account: null, session: null });
  },

  setSession: (account, session) => {
    clearStoredToken();
    set({ status: "signedIn", mode: "session", token: null, account, session });
  },

  checkSession: async () => {
    if (get().mode === "token") return;
    try {
      const res = await fetch("/v1/auth/session", {
        headers: { Accept: "application/json", "X-Throughline-Client": "web" },
        credentials: "same-origin",
        cache: "no-store",
      });
      const body = res.ok ? ((await res.json()) as SessionAnswer) : null;
      if (body?.account && body.session) {
        get().setSession(body.account, body.session);
        return;
      }
    } catch {
      /* server unreachable or older than sign-in: fall through */
    }
    if (get().mode !== "token") set({ status: "signedOut", mode: null, account: null, session: null });
  },

  signOut: () => {
    const wasSession = get().mode === "session";
    clearStoredToken();
    try {
      sessionStorage.removeItem(ASKER_KEY);
    } catch {
      /* ignore */
    }
    if (wasSession) {
      // Best effort: the cookie is HttpOnly, so only the server can end it.
      void fetch("/v1/auth/logout", {
        method: "POST",
        headers: { "X-Throughline-Client": "web" },
        credentials: "same-origin",
      }).catch(() => undefined);
    }
    set({ status: "signedOut", mode: null, token: null, remember: false, account: null, session: null, actingAsker: null });
  },

  setActingAsker: (asker) => {
    try {
      if (asker) sessionStorage.setItem(ASKER_KEY, asker);
      else sessionStorage.removeItem(ASKER_KEY);
    } catch {
      /* ignore */
    }
    set({ actingAsker: asker });
  },
}));

/** Non-React access for the fetch client. */
export const auth = {
  token: (): string | null => useAuth.getState().token,
  asker: (): string | null => useAuth.getState().actingAsker,
  signOut: (): void => useAuth.getState().signOut(),
};
