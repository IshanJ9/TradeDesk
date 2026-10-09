import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { actionTitle, priceLine, validityWord } from "./describe";
import { fingerprint } from "./fingerprint";
import { clock, indianGroup, pct, rupees, secondsLeft, shortHash } from "./format";
import { AUDIT_LIMIT, awaiting, initialState, reducer, resolved, type State } from "./store";
import type { WsEvent } from "./types";
import { isContiguous, wsUrl } from "./ws";

// Loose fixtures: these tests are about how events change state, not about every field.
const fx = <T>(o: object) => o as unknown as T;
const ev = (e: object) => ({ type: "event", event: e as unknown as WsEvent }) as const;

const pending = (id: string, state = "PENDING", created = "2026-10-08T10:00:00Z") =>
  ({ id, state, created_at: created, order_hash: "ab".repeat(32) }) as never;
const plan = (id: string, state = "PENDING", created = "2026-10-08T10:00:00Z") =>
  ({ id, state, created_at: created, plan_hash: "cd".repeat(32) }) as never;

describe("money formatting", () => {
  it("groups digits the Indian way", () => {
    expect(indianGroup(0)).toBe("0");
    expect(indianGroup(999)).toBe("999");
    expect(indianGroup(1000)).toBe("1,000");
    expect(indianGroup(100000)).toBe("1,00,000");
    expect(indianGroup(12345678)).toBe("1,23,45,678");
  });
  it("formats paise as rupees, with a real minus and optional plus", () => {
    expect(rupees(145000)).toBe("₹1,450.00");
    expect(rupees(1452273)).toBe("₹14,522.73");
    expect(rupees(5)).toBe("₹0.05");
    expect(rupees(-71000)).toBe("−₹710.00");
    expect(rupees(250000, { plus: true })).toBe("+₹2,500.00");
    expect(rupees(0, { plus: true })).toBe("₹0.00");
    expect(rupees(1000000000)).toBe("₹1,00,00,000.00");
  });
  it("formats percentages", () => {
    expect(pct(-7.24)).toBe("−7.24%");
    expect(pct(0.56)).toBe("+0.56%");
    expect(pct(0)).toBe("0.00%");
  });
  it("counts down to an expiry and never goes negative", () => {
    const t = Date.parse("2026-10-08T10:00:00Z");
    expect(secondsLeft("2026-10-08T10:01:00Z", t)).toBe(60);
    expect(secondsLeft("2026-10-08T10:01:00Z", t + 59_500)).toBe(1);
    expect(secondsLeft("2026-10-08T10:01:00Z", t + 61_000)).toBe(0);
  });
  it("shortens a hash into readable groups", () => {
    expect(shortHash("0123456789abcdef0123")).toBe("0123 4567 89ab cdef");
  });
  it("formats a time of day", () => {
    expect(clock("2026-10-08T10:05:09Z")).toMatch(/\d{2}:\d{2}:\d{2}/);
  });
});

describe("fingerprint", () => {
  const h = "6f9656c48595ea87" + "1234567890abcdef";
  it("is the same picture for the same hash and a different one when anything changes", () => {
    expect(fingerprint(h)).toEqual(fingerprint(h));
    expect(fingerprint(h)).toHaveLength(16);
    expect(fingerprint(h)).not.toEqual(fingerprint("7" + h.slice(1)));
    expect(fingerprint(h)).not.toEqual(fingerprint(h.slice(0, -1) + "0"));
  });
  it("stays in range and copes with short or odd input", () => {
    for (const c of fingerprint(h)) {
      expect(c.hue).toBeGreaterThanOrEqual(0);
      expect(c.hue).toBeLessThan(360);
      expect(c.height).toBeGreaterThanOrEqual(0.35);
      expect(c.height).toBeLessThanOrEqual(1);
    }
    expect(fingerprint("")).toHaveLength(16);
    expect(fingerprint("zz!!")).toHaveLength(16);
  });
});

describe("live feed helpers", () => {
  it("builds the websocket address from the page", () => {
    expect(wsUrl({ protocol: "http:", host: "localhost:5173" })).toBe("ws://localhost:5173/ws");
    expect(wsUrl({ protocol: "https:", host: "desk.example" })).toBe("wss://desk.example/ws");
  });
  it("detects a missed event", () => {
    expect(isContiguous(null, { type: "tick", seq: 90 })).toBe(true);
    expect(isContiguous(5, { type: "tick", seq: 6 })).toBe(true);
    expect(isContiguous(5, { type: "tick", seq: 7 })).toBe(false);
    expect(isContiguous(5, { type: "tick", seq: 5 })).toBe(false);
    expect(isContiguous(50, { type: "snapshot", seq: 3 })).toBe(true); // a snapshot restarts the sequence
  });
});

describe("store", () => {
  const apply = (s: State, e: object) => reducer(s, ev(e));

  it("loads everything from a snapshot", () => {
    const s = apply(initialState, {
      type: "snapshot", seq: 0,
      account: { funds: { available_cash: 1 }, holdings: [], positions: [], locks: {} },
      pending: { orders: [pending("p1")], plans: [plan("pl1")] },
      orders: [{ order_id: "o1" }], rules: [{ id: "r1" }],
    });
    expect(s.loaded).toBe(true);
    expect(Object.keys(s.pending)).toEqual(["p1"]);
    expect(Object.keys(s.plans)).toEqual(["pl1"]);
    expect(s.orders).toHaveLength(1);
    expect(s.rules).toHaveLength(1);
  });

  it("keeps cards that already resolved when a fresh snapshot arrives after a reconnect", () => {
    let s = apply(initialState, { type: "pending_updated", seq: 1, pending: pending("done", "SENT") });
    s = apply(s, { type: "snapshot", seq: 0, account: {}, pending: { orders: [pending("p2")], plans: [] }, orders: [], rules: [] });
    expect(Object.keys(s.pending).sort()).toEqual(["done", "p2"]);
  });

  it("drops a card that was still waiting if the server no longer has it", () => {
    let s = apply(initialState, { type: "pending_created", seq: 1, pending: pending("gone") });
    s = apply(s, { type: "snapshot", seq: 0, account: {}, pending: { orders: [], plans: [] }, orders: [], rules: [] });
    expect(s.pending).toEqual({});
  });

  it("tracks prices and account updates", () => {
    let s = apply(initialState, { type: "tick", seq: 1, tick: { instrument_key: "NSE:INFY", ltp: 144800, seq: 4 } });
    expect(s.ticks["NSE:INFY"]).toEqual({ ltp: 144800, seq: 4 });
    s = apply(s, { type: "account_update", seq: 2, account: { funds: { available_cash: 9 } } });
    expect(s.account?.funds.available_cash).toBe(9);
    s = apply(s, { type: "lock_update", seq: 3, locks: { anchor_active: true } });
    expect(s.account?.locks.anchor_active).toBe(true);
  });

  it("ignores a lock update before there is an account", () => {
    expect(apply(initialState, { type: "lock_update", seq: 1, locks: {} }).account).toBeNull();
  });

  it("updates an order in place and puts a new one first", () => {
    let s = apply(initialState, { type: "order_update", seq: 1, order: { order_id: "o1", status: "OPEN" } });
    s = apply(s, { type: "order_update", seq: 2, order: { order_id: "o2", status: "OPEN" } });
    s = apply(s, { type: "order_update", seq: 3, order: { order_id: "o1", status: "FILLED" } });
    expect(s.orders.map((o) => [o.order_id, o.status])).toEqual([["o2", "OPEN"], ["o1", "FILLED"]]);
  });

  it("moves a card from waiting to resolved when the server says so", () => {
    let s = apply(initialState, { type: "pending_created", seq: 1, pending: pending("p1") });
    expect(awaiting(s).orders.map((p) => p.id)).toEqual(["p1"]);
    s = apply(s, { type: "pending_updated", seq: 2, pending: pending("p1", "VOID") });
    expect(awaiting(s).orders).toEqual([]);
    expect(resolved(s).orders.map((p) => p.id)).toEqual(["p1"]);
  });

  it("lists waiting cards oldest first and resolved cards newest first", () => {
    let s = initialState;
    s = apply(s, { type: "pending_created", seq: 1, pending: pending("b", "PENDING", "2026-10-08T10:00:02Z") });
    s = apply(s, { type: "pending_created", seq: 2, pending: pending("a", "PENDING", "2026-10-08T10:00:01Z") });
    expect(awaiting(s).orders.map((p) => p.id)).toEqual(["a", "b"]);
    s = apply(s, { type: "pending_updated", seq: 3, pending: pending("a", "SENT", "2026-10-08T10:00:01Z") });
    s = apply(s, { type: "pending_updated", seq: 4, pending: pending("b", "SENT", "2026-10-08T10:00:02Z") });
    expect(resolved(s).orders.map((p) => p.id)).toEqual(["b", "a"]);
  });

  it("lets the trader dismiss a resolved card, and remembers what happened to it", () => {
    let s = apply(initialState, { type: "pending_updated", seq: 1, pending: pending("p1", "SENT") });
    s = reducer(s, { type: "result", id: "p1", result: fx({ outcome: "SENT" }) });
    expect(s.results["p1"]?.outcome).toBe("SENT");
    s = reducer(s, { type: "hide", id: "p1" });
    expect(resolved(s).orders).toEqual([]);
  });

  it("sets and clears a banner on a card", () => {
    let s = reducer(initialState, { type: "note", id: "p1", note: { tone: "warn", text: "Price moved" } });
    expect(s.notes["p1"]?.text).toBe("Price moved");
    s = reducer(s, { type: "note", id: "p1", note: null });
    expect(s.notes).toEqual({});
  });

  it("tracks plans and their reports", () => {
    let s = apply(initialState, { type: "plan_created", seq: 1, plan: plan("pl1") });
    expect(awaiting(s).plans.map((p) => p.id)).toEqual(["pl1"]);
    s = apply(s, { type: "plan_updated", seq: 2, plan: plan("pl1", "RUNNING") });
    s = apply(s, { type: "plan_report_update", seq: 3, report: { plan_id: "pl1", state: "RUNNING", legs: [], summary: "x" } });
    expect(awaiting(s).plans).toEqual([]);
    expect(resolved(s).plans[0]?.state).toBe("RUNNING");
    expect(s.reports["pl1"]?.summary).toBe("x");
  });

  it("never lets an older HTTP report replace newer live progress", () => {
    // the live feed reported the finished steps first; the 'approve' answer (a start-of-run report) lands later
    let s = apply(initialState, { type: "plan_report_update", seq: 5, report: { plan_id: "pl1", state: "COMPLETED", legs: [], summary: "done" } });
    s = reducer(s, { type: "report", report: fx({ plan_id: "pl1", state: "APPROVED", legs: [], summary: "starting" }) });
    expect(s.reports["pl1"]?.summary).toBe("done");
    // but it does fill in a report the live feed hasn't delivered yet
    const fresh = reducer(initialState, { type: "report", report: fx({ plan_id: "pl2", state: "APPROVED", legs: [], summary: "starting" }) });
    expect(fresh.reports["pl2"]?.summary).toBe("starting");
  });

  it("keeps a plan's steps inside the plan instead of showing them as separate tickets", () => {
    const step = (id: string, state: string) => ({ id, state, plan_id: "pl1", created_at: "2026-10-08T10:00:00Z" });
    let s = apply(initialState, { type: "pending_updated", seq: 1, pending: step("leg1", "SENT") });
    s = apply(s, { type: "pending_updated", seq: 2, pending: step("leg2", "PENDING") });
    s = apply(s, { type: "pending_updated", seq: 3, pending: pending("alone", "SENT") });
    expect(awaiting(s).orders).toEqual([]);
    expect(resolved(s).orders.map((p) => p.id)).toEqual(["alone"]);
  });

  it("raises a toast when a rule fires and links it to its card", () => {
    const s = apply(initialState, {
      type: "rule_fired", seq: 1, message: "Your rule fired", ltp: 1, pending: pending("p9"),
      rule: { id: "r1", status: "FIRED" },
    });
    expect(s.toasts).toHaveLength(1);
    expect(s.toasts[0]).toMatchObject({ kind: "rule", message: "Your rule fired", cardId: "p9" });
    expect(s.rules[0]?.status).toBe("FIRED");
    const gone = reducer(s, { type: "dismissToast", id: s.toasts[0]!.id });
    expect(gone.toasts).toEqual([]);
  });

  it("updates a rule in place", () => {
    let s = apply(initialState, { type: "rule_update", seq: 1, rule: { id: "r1", status: "ACTIVE" } });
    s = apply(s, { type: "rule_update", seq: 2, rule: { id: "r1", status: "CANCELLED" } });
    expect(s.rules).toHaveLength(1);
    expect(s.rules[0]?.status).toBe("CANCELLED");
  });

  it("keeps the audit log newest first and bounded", () => {
    let s = initialState;
    for (let i = 0; i < AUDIT_LIMIT + 20; i++) s = apply(s, { type: "audit_event", seq: i, event: { id: `a${i}` } });
    expect(s.audit).toHaveLength(AUDIT_LIMIT);
    expect(s.audit[0]?.id).toBe(`a${AUDIT_LIMIT + 19}`);
  });

  it("tracks connection state", () => {
    expect(reducer(initialState, { type: "conn", status: "live" }).conn).toBe("live");
  });
});

describe("stop-loss wording", () => {
  const stop = { side: "SELL" as const, limit_price: 139000, protection_price: null, trigger_price: 140000 };

  it("says nothing happens until the price reaches the trigger", () => {
    expect(priceLine(stop)).toBe("Stop-loss: if the price falls to ₹1,400.00, sell at least ₹1,390.00");
    expect(priceLine({ ...stop, side: "BUY", limit_price: 141000, trigger_price: 140000 })).toContain("if the price rises to ₹1,400.00, buy up to ₹1,410.00");
  });

  it("still words a plain limit and a protected market order as before", () => {
    expect(priceLine({ side: "BUY", limit_price: 145000, protection_price: null, trigger_price: null })).toBe("Limit: up to ₹1,450.00");
    expect(priceLine({ side: "SELL", limit_price: null, protection_price: 140000, trigger_price: null })).toContain("won't fill below");
  });

  it("titles a stop-loss card as one, and a change to it", () => {
    const base = { instrument: { symbol: "INFY" }, quantity: 5, side: "SELL", target_order_id: "9", order_type: "STOP_LIMIT" };
    expect(actionTitle({ ...base, action: "PLACE" } as never)).toBe("Stop-loss · Sell 5 × INFY");
    expect(actionTitle({ ...base, action: "MODIFY" } as never)).toBe("Change stop-loss 9");
  });

  it("knows only the two validities 021 offers", () => {
    expect(validityWord("DAY")).toBe("Valid for the day");
    expect(validityWord("IOC")).toBe("Fill now or cancel");
  });
});

describe("api client", () => {
  afterEach(() => vi.unstubAllGlobals());
  const respond = (status: number, body: unknown) =>
    vi.stubGlobal("fetch", vi.fn(async () => new Response(typeof body === "string" ? body : JSON.stringify(body), { status })));

  it("returns data on success and sends the hash the trader saw", async () => {
    respond(200, { outcome: "SENT" });
    const r = await api.approveOrder("p 1", "h".repeat(64));
    expect(r).toEqual({ ok: true, data: { outcome: "SENT" } });
    const [url, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("/api/approvals/p%201/approve");
    expect(JSON.parse(init.body)).toEqual({ order_hash: "h".repeat(64) });
  });

  it("turns a 409 into a typed conflict with the reason", async () => {
    respond(409, { code: "REQUOTE_REQUIRED", message: "The price moved.", pending: { id: "new" } });
    const r = await api.approveOrder("p1", "h".repeat(64));
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.status).toBe(409);
      expect(r.conflict?.code).toBe("REQUOTE_REQUIRED");
      expect(r.message).toBe("The price moved.");
    }
  });

  it("explains unreachable servers and unexpected errors in plain words", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("network"); }));
    const down = await api.chat("hi");
    expect(!down.ok && down.status === 0 && down.message.includes("Can't reach the server")).toBe(true);

    respond(503, { detail: "broker unreachable" });
    const unavailable = await api.chat("hi");
    expect(!unavailable.ok && unavailable.message.includes("Nothing was sent")).toBe(true);

    respond(404, { detail: "unknown approval id" });
    const missing = await api.declineOrder("x");
    expect(!missing.ok && missing.message).toBe("unknown approval id");

    respond(500, "boom");
    const broken = await api.cancelRule("r1");
    expect(!broken.ok && broken.message).toBe("Request failed (500).");
  });

  it("uses the right routes for plans and rules", async () => {
    respond(200, {});
    await api.approvePlan("pl1", "p".repeat(64));
    await api.cancelRule("r-1");
    const calls = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.map((c) => [c[1].method, c[0]]);
    expect(calls).toEqual([["POST", "/api/plans/pl1/approve"], ["DELETE", "/api/rules/r-1"]]);
  });
});
