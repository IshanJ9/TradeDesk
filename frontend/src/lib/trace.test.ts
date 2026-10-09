import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { AssistantTrace } from "../components/AssistantTrace";
import { ExternalOrders } from "../components/ExternalOrders";
import { initialState } from "./store";
import { traceLabel, traceRuns } from "./trace";
import type { Order, TraceEvent } from "./types";

const event = (seq: number, status: TraceEvent["status"], overrides: Partial<TraceEvent> = {}): TraceEvent => ({
  type: "trace", seq, run_id: "a", node: "router", kind: "node", status, detail: "", ms: null, ...overrides,
});

describe("trace grouping", () => {
  it("pairs starts and ends, showing only completed duration", () => {
    const runs = traceRuns([event(2, "end", { ms: 12 }), event(1, "start")]);
    expect(runs[0]?.steps).toHaveLength(1);
    expect(runs[0]?.steps[0]).toMatchObject({ status: "done", ms: 12 });
    expect(runs[0]?.totalMs).toBe(12);
  });
  it("keeps repeated calls separate and sorts new runs before old late events", () => {
    const runs = traceRuns([
      event(6, "end", { ms: 4 }), event(5, "start"),
      event(4, "start", { run_id: "b" }), event(3, "end", { ms: 7 }), event(1, "start"),
    ]);
    expect(runs.map((run) => run.id)).toEqual(["b", "a"]);
    expect(runs[1]?.steps).toHaveLength(2);
    expect(runs[1]?.totalMs).toBe(11);
    expect(runs[0]?.steps[0]?.status).toBe("running");
  });
  it("supports missing starts, blocked guards and duplicate events", () => {
    const blocked = event(4, "blocked", { node: "output_guard", kind: "guard", detail: "Unsupported number", ms: 3 });
    const runs = traceRuns([blocked, blocked]);
    expect(runs[0]?.steps).toHaveLength(1);
    expect(runs[0]?.steps[0]).toMatchObject({ status: "blocked", detail: "Unsupported number" });
    expect(runs[0]?.totalMs).toBe(3);
  });
  it("preserves errors and unknown timings without inventing a duration", () => {
    const run = traceRuns([event(1, "error", { ms: -1 })])[0]!;
    expect(run.steps[0]?.status).toBe("error");
    expect(run.steps[0]?.ms).toBeNull();
    expect(run.totalMs).toBe(0);
  });
  it.each([
    ["input_guard", "Checked your message"], ["router", "Decided what you're asking"],
    ["output_guard", "Checked the reply"], ["tool:get_positions", "Looked up get positions"],
    ["future_node", "future_node"],
  ])("labels %s", (node, expected) => expect(traceLabel(node)).toBe(expected));
  it("does not mutate reducer events", () => {
    const events = Object.freeze([Object.freeze(event(2, "end")), Object.freeze(event(1, "start"))]);
    traceRuns([...events]);
    expect(events[0]?.seq).toBe(2);
  });
});

describe("trace and external-order markup", () => {
  it("expands newest run, collapses older runs and labels demo data", () => {
    const html = renderToStaticMarkup(createElement(AssistantTrace, { state: { ...initialState, trace: [
      event(2, "blocked", { run_id: "demo-new", detail: '<img src=x onerror="alert(1)">', ms: 3 }),
      event(1, "end", { run_id: "old", ms: 4 }),
    ] } }));
    expect(html.indexOf("demo-new")).toBeLessThan(html.indexOf("Run old"));
    expect(html).toContain('aria-expanded="true"');
    expect(html).toContain('aria-expanded="false"');
    expect(html).toContain("DEMO DATA");
    expect(html).toContain("&lt;img");
    expect(html).not.toContain("<img");
    expect(html).toContain("Recorded time 3 ms");
  });
  const order = {
    order_id: "external-1", instrument: { symbol: "INFY", exchange: "NSE" }, side: "BUY",
    quantity: 10, filled_quantity: 3, avg_fill_price: 145005, order_type: "LIMIT", limit_price: 145010,
    trigger_price: null, product: "CNC", status: "UNKNOWN", created_at: "2026-10-09T05:00:00Z",
    updated_at: "2026-10-09T05:01:00Z",
  } as Order;
  it("shows unknown honestly, with price, fills, source and times", () => {
    const html = renderToStaticMarkup(createElement(ExternalOrders, { state: { ...initialState, external: [order] } }));
    expect(html).toContain("Unknown — not confirmed");
    expect(html).toContain("3 of 10 filled");
    expect(html).toContain("₹1,450.05");
    expect(html).toContain("₹1,450.10");
    expect(html).toContain("INFY");
    expect(html).toContain("counts toward your daily activity");
    expect(html).toContain('dateTime="2026-10-09T05:01:00Z"');
  });
  it("does not claim an empty session proves there were no outside orders", () => {
    const html = renderToStaticMarkup(createElement(ExternalOrders, { state: initialState }));
    expect(html).toContain("Loading saved activity");
    expect(html).not.toContain("No external orders recorded");
  });
});
