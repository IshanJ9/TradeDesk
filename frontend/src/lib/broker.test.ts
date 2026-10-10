import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { linkBroker, reconnectBroker, refreshBroker, unlinkBroker, forgetBroker, sessionStateForTest } from "./broker";
import { brokerApi } from "./api";

const mock = { kind: "mock", status: "mock", ucc_hint: null, server_account: false, can_link: true, link_unavailable_reason: null };
const linked = { kind: "021", status: "connected", ucc_hint: "…1234", server_account: false, can_link: true, link_unavailable_reason: null };
const reply = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });

let calls: Array<[string, RequestInit]>;
const fetchReturning = (...replies: Response[]) => {
  calls = [];
  const queue = [...replies];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => { calls.push([url, init]); return queue.shift() ?? reply(200, {}); }));
};

beforeEach(() => forgetBroker());
afterEach(() => vi.unstubAllGlobals());

describe("broker account", () => {
  it("keeps the server's own words when linking is refused, and does not claim a link", async () => {
    fetchReturning(reply(422, { detail: "021 did not accept those details. Nothing was saved." }));
    const r = await linkBroker("HACK1234", "pw");
    expect(r).toEqual({ ok: false, message: "021 did not accept those details. Nothing was saved." });
    expect(sessionStateForTest()).toBeNull();
  });

  it("explains an unreachable 021 and a lock-out in the server's words", async () => {
    fetchReturning(reply(503, { detail: "Couldn't reach 021 to check those details. Nothing was saved." }));
    expect(await linkBroker("A", "b")).toMatchObject({ ok: false, message: expect.stringContaining("Couldn't reach 021") });
    fetchReturning(reply(429, { detail: "Too many attempts. Try again in a few minutes." }));
    expect(await linkBroker("A", "b")).toMatchObject({ ok: false, message: expect.stringContaining("Too many attempts") });
  });

  it("sends the login only in the request body, with the cookie and CSRF header, and keeps only the status", async () => {
    fetchReturning(reply(200, linked));
    const r = await linkBroker("HACK1234", "secret-pw");
    expect(r).toEqual({ ok: true });
    const [url, init] = calls[0]!;
    expect(url).toBe("/api/broker/link");
    expect(init.credentials).toBe("include");
    expect(JSON.parse(init.body as string)).toEqual({ username: "HACK1234", password: "secret-pw" });
    expect(JSON.stringify(sessionStateForTest())).not.toContain("secret-pw");
    expect(sessionStateForTest()).toEqual(linked);
  });

  it("unlink and reconnect use their routes and update the status", async () => {
    fetchReturning(reply(200, mock), reply(200, { ...linked, status: "connected" }));
    await unlinkBroker();
    expect(sessionStateForTest()).toEqual(mock);
    await reconnectBroker();
    const routes = calls.map(([url, init]) => [init.method, url]);
    expect(routes).toEqual([["DELETE", "/api/broker/link"], ["POST", "/api/broker/reconnect"]]);
  });

  it("a status refresh replaces what is shown, and a failed refresh leaves it alone", async () => {
    fetchReturning(reply(200, linked));
    await refreshBroker();
    expect(sessionStateForTest()).toEqual(linked);
    fetchReturning(reply(500, "boom"));
    await refreshBroker();
    expect(sessionStateForTest()).toEqual(linked);
    expect(brokerApi.status).toBeTypeOf("function");
  });
});
