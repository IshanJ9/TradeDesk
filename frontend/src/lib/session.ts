// Who is signed in. The browser holds an HttpOnly cookie it cannot read; this module holds the one thing the page
// needs to send back on every change: the CSRF token the server returned at sign-in (kept in memory, never in
// storage, so a script injected into another site cannot find it).
import { useSyncExternalStore } from "react";
import type { components } from "./types.gen";
import { navigate } from "./router";

type Schemas = components["schemas"];
export type Me = Schemas["Me"];
type SessionReply = Schemas["Session"];

export interface SessionState {
  status: "loading" | "signedOut" | "signedIn";
  user: Me | null;
}

let csrf: string | null = null;
let state: SessionState = { status: "loading", user: null };
const listeners = new Set<() => void>();

function set(next: SessionState): void {
  state = next;
  listeners.forEach((l) => l());
}

export function sessionState(): SessionState {
  return state;
}

export function useSession(): SessionState {
  return useSyncExternalStore((l) => { listeners.add(l); return () => { listeners.delete(l); }; }, () => state);
}

/** Headers every request that changes something must carry. */
export function csrfHeaders(): Record<string, string> {
  return csrf ? { "X-CSRF-Token": csrf } : {};
}

/** The server no longer knows this session (expired, signed out elsewhere, password changed): back to the log-in page. */
export function sessionLost(): void {
  csrf = null;
  if (state.status !== "signedOut") set({ status: "signedOut", user: null });
  navigate("login");
}

export type AuthResult<T> = { ok: true; data: T } | { ok: false; status: number; message: string };

async function authRequest<T>(method: string, url: string, body?: unknown): Promise<AuthResult<T>> {
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      credentials: "include",
      headers: { ...(body === undefined ? {} : { "Content-Type": "application/json" }), ...csrfHeaders() },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    return { ok: false, status: 0, message: "Can't reach the server. Check that the backend is running." };
  }
  const text = await res.text();
  let json: unknown;
  try { json = text ? JSON.parse(text) : undefined; } catch { /* not JSON */ }
  if (res.ok) return { ok: true, data: json as T };
  const detail = (json as { detail?: unknown } | undefined)?.detail;
  if (res.status === 429) {
    const wait = Number(res.headers.get("Retry-After"));
    const mins = Number.isFinite(wait) && wait > 0 ? Math.ceil(wait / 60) : null;
    return { ok: false, status: 429, message: mins ? `Too many attempts. Try again in ${mins} minute${mins === 1 ? "" : "s"}.` : "Too many attempts. Try again in a few minutes." };
  }
  if (typeof detail === "string") return { ok: false, status: res.status, message: detail };
  if (res.status === 422) return { ok: false, status: 422, message: "Please check what you typed and try again." };
  return { ok: false, status: res.status, message: `Request failed (${res.status}).` };
}

function accept(r: AuthResult<SessionReply>): AuthResult<Me> {
  if (!r.ok) return r;
  csrf = r.data.csrf_token;
  set({ status: "signedIn", user: r.data.user });
  return { ok: true, data: r.data.user };
}

/** Ask the server who we are (page load). A 401 here is normal: nobody is signed in. */
export async function loadSession(): Promise<void> {
  const r = await authRequest<SessionReply>("GET", "/api/auth/me");
  if (r.ok) accept(r);
  else if (r.status === 401) { csrf = null; set({ status: "signedOut", user: null }); }
  else set({ status: "signedOut", user: null }); // can't reach the server: treat as signed out; the log-in page says why on submit
}

export const signIn = async (email: string, password: string) =>
  accept(await authRequest<SessionReply>("POST", "/api/auth/login", { email, password }));

export const signUp = async (email: string, password: string, displayName: string) =>
  accept(await authRequest<SessionReply>("POST", "/api/auth/register", { email, password, display_name: displayName, accepts_no_advice: true }));

export async function signOut(): Promise<void> {
  await authRequest<void>("POST", "/api/auth/logout");
  csrf = null;
  set({ status: "signedOut", user: null });
  navigate("landing");
}

export const changePassword = async (current: string, next: string) =>
  accept(await authRequest<SessionReply>("POST", "/api/auth/change-password", { current_password: current, new_password: next }));
