import { afterEach, describe, expect, it, vi } from "vitest";
import { call } from "./DemoControls";
import { loadSession } from "../lib/session";

afterEach(() => vi.unstubAllGlobals());

describe("demo controls", () => {
  it("send the session cookie and the CSRF token, or a signed-in server refuses every demo action", async () => {
    const calls: Array<[string, RequestInit]> = [];
    vi.stubGlobal("window", { location: { pathname: "/app" }, history: { pushState() {} }, dispatchEvent: () => true, scrollTo() {} });
    vi.stubGlobal("PopStateEvent", class {});
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
      calls.push([url, init]);
      if (url === "/api/auth/me") return new Response(JSON.stringify({ user: { id: "u1", email: "a@b.c", display_name: "A" }, csrf_token: "tok-demo" }), { status: 200 });
      return new Response(JSON.stringify({ network_down: false, anchor_active: false, timeout_armed: false }), { status: 200 });
    }));
    await loadSession();
    await call("price-jump", {});
    await call("status");
    const [, post] = calls.find(([u]) => u === "/api/demo/price-jump")!;
    expect(post.method).toBe("POST");
    expect(post.credentials).toBe("include");
    expect(post.headers).toMatchObject({ "X-CSRF-Token": "tok-demo", "content-type": "application/json" });
    const [, get] = calls.find(([u]) => u === "/api/demo/status")!;
    expect(get.method).toBe("GET");
    expect(get.credentials).toBe("include");
  });
});
