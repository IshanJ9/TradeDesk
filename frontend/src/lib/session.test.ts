import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { changePassword, csrfHeaders, loadSession, sessionState, signIn, signOut, signUp } from "./session";

const me = { id: "u1", email: "ada@example.com", display_name: "Ada" };
const session = (token = "csrf-1") => ({ user: me, csrf_token: token });
const reply = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(body === undefined ? null : JSON.stringify(body), { status, headers });

let calls: Array<[string, RequestInit]>;
let pushed: string[];

function fetchReturning(...replies: Response[]) {
  calls = [];
  const queue = [...replies];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
    calls.push([url, init]);
    return queue.shift() ?? reply(200, {});
  }));
}

beforeEach(() => {
  pushed = [];
  vi.stubGlobal("window", {
    location: { pathname: "/app" },
    history: { pushState: (_s: unknown, _t: string, path: string) => pushed.push(path) },
    dispatchEvent: () => true,
    scrollTo: () => {},
  });
  vi.stubGlobal("PopStateEvent", class {});
});

afterEach(async () => {
  fetchReturning(reply(204, undefined));
  await signOut(); // leaves the module signed out for the next test
  vi.unstubAllGlobals();
});

describe("signing in", () => {
  it("sends the credentials once, keeps only the user and the CSRF token, and sends that token on later changes", async () => {
    fetchReturning(reply(200, session("tok-123")), reply(200, { outcome: "SENT" }));
    const r = await signIn("ada@example.com", "a decent passphrase");
    expect(r).toEqual({ ok: true, data: me });
    expect(sessionState()).toEqual({ status: "signedIn", user: me }); // no password anywhere in the state
    expect(JSON.stringify(sessionState())).not.toContain("passphrase");
    expect(calls[0]![0]).toBe("/api/auth/login");
    expect(calls[0]![1].credentials).toBe("include");

    await api.approveOrder("p1", "h".repeat(64));
    const [, init] = calls[1]!;
    expect(init.credentials).toBe("include");
    expect((init.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok-123");
  });

  it("shows the server's own words for a wrong password, a taken email and a lock-out", async () => {
    fetchReturning(reply(401, { detail: "Incorrect email or password." }));
    expect(await signIn("a@b.co", "x")).toEqual({ ok: false, status: 401, message: "Incorrect email or password." });
    expect(sessionState().status).not.toBe("signedIn");

    fetchReturning(reply(409, { detail: "That email already has an account. Log in instead." }));
    const taken = await signUp("a@b.co", "a decent passphrase", "Ada");
    expect(!taken.ok && taken.message).toContain("already has an account");

    fetchReturning(reply(429, { detail: "Too many attempts." }, { "Retry-After": "181" }));
    const locked = await signIn("a@b.co", "x");
    expect(!locked.ok && locked.message).toBe("Too many attempts. Try again in 4 minutes.");
  });

  it("registers with the no-advice acknowledgment the server insists on", async () => {
    fetchReturning(reply(201, session()));
    await signUp("ada@example.com", "a decent passphrase", "Ada");
    expect(JSON.parse(calls[0]![1].body as string)).toEqual({
      email: "ada@example.com", password: "a decent passphrase", display_name: "Ada", accepts_no_advice: true,
    });
  });
});

describe("page load and sign-out", () => {
  it("a 401 on load just means nobody is signed in: no redirect, no CSRF token", async () => {
    fetchReturning(reply(401, { detail: "Sign in to continue." }));
    await loadSession();
    expect(sessionState().status).toBe("signedOut");
    expect(csrfHeaders()).toEqual({});
    expect(pushed).toEqual([]);
  });

  it("a valid session on load restores the user and the token", async () => {
    fetchReturning(reply(200, session("tok-load")));
    await loadSession();
    expect(sessionState().user).toEqual(me);
    expect(csrfHeaders()).toEqual({ "X-CSRF-Token": "tok-load" });
  });

  it("logging out forgets the token and goes to the landing page", async () => {
    fetchReturning(reply(200, session("tok-out")), reply(204, undefined));
    await signIn("ada@example.com", "a decent passphrase");
    await signOut();
    expect(csrfHeaders()).toEqual({});
    expect(sessionState()).toEqual({ status: "signedOut", user: null });
    expect(pushed).toEqual(["/"]);
  });
});

describe("a session that ends while the desk is open", () => {
  it("any 401 clears the user and goes to the log-in page", async () => {
    fetchReturning(reply(200, session("tok-x")), reply(401, { detail: "Sign in to continue." }));
    await signIn("ada@example.com", "a decent passphrase");
    const r = await api.chat("hello");
    expect(r).toMatchObject({ ok: false, status: 401 });
    expect(sessionState().status).toBe("signedOut");
    expect(csrfHeaders()).toEqual({});
    expect(pushed).toEqual(["/login"]);
  });
});

describe("changing the password", () => {
  it("replaces the session (the server signed this device out and back in) and reports a wrong current password", async () => {
    fetchReturning(reply(200, session("old")), reply(200, session("new")));
    await signIn("ada@example.com", "a decent passphrase");
    await changePassword("a decent passphrase", "another good passphrase");
    expect(csrfHeaders()).toEqual({ "X-CSRF-Token": "new" });
    fetchReturning(reply(403, { detail: "The current password is not right." }));
    const bad = await changePassword("nope nope nope", "another good passphrase");
    expect(!bad.ok && bad.message).toBe("The current password is not right.");
    expect(sessionState().status).toBe("signedIn"); // a failed attempt does not sign anyone out
  });
});
