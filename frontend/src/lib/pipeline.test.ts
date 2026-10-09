import { describe, expect, it } from "vitest";
import { pipelineOf } from "./pipeline";
import type { TraceRun, TraceStep } from "./trace";

let seq = 0;
const step = (node: string, kind: TraceStep["kind"], status: TraceStep["status"], detail = "", ms: number | null = 5): TraceStep =>
  ({ seq: ++seq, node, kind, status, detail, ms });
const run = (...steps: TraceStep[]): TraceRun => ({ id: "r1", firstSeq: 1, demo: false, steps, totalMs: 0 });

describe("pipelineOf", () => {
  it("an order: every node done, tools named, route kept, reply reached", () => {
    const p = pipelineOf(run(
      step("input_guard", "guard", "done"), step("router", "node", "done", "may prepare a card for your approval", null),
      step("model", "node", "done"), step("tool:propose_order", "tool", "done"), step("model", "node", "done"),
      step("output_guard", "guard", "done"),
    ));
    expect(p.states).toMatchObject({ input_guard: "done", router: "done", model: "done", tools: "done", output_guard: "done", reply: "done" });
    expect(p.tools.map((t) => t.name)).toEqual(["propose order"]);
    expect(p.modelRounds).toBe(2);
    expect(p.route).toBe("may prepare a card for your approval");
    expect(p.finished && p.blocked === null).toBe(true);
  });

  it("a trick message: the input guard blocks and everything after is skipped", () => {
    const p = pipelineOf(run(step("input_guard", "guard", "done"), step("input_guard", "guard", "blocked", "message tried to change the assistant's rules")));
    expect(p.states.input_guard).toBe("blocked");
    expect(p.states.model).toBe("skipped");
    expect(p.blocked?.detail).toContain("change the assistant's rules");
    expect(p.finished).toBe(true);
  });

  it("a drafting tool refused on a question shows the blocked chip", () => {
    const p = pipelineOf(run(
      step("input_guard", "guard", "done"), step("router", "node", "done"), step("model", "node", "done"),
      step("tool:propose_order", "guard", "blocked", "not available for a question"), step("model", "node", "done"),
      step("output_guard", "guard", "done"),
    ));
    expect(p.tools).toEqual([{ name: "propose order", state: "blocked", ms: null }]);
    expect(p.blocked?.node).toBe("tools");
  });

  it("still working: the model is running and nothing is finished", () => {
    const p = pipelineOf(run(step("input_guard", "guard", "done"), step("router", "node", "done"), step("model", "node", "running", "step 1", null)));
    expect(p.states.model).toBe("running");
    expect(p.finished).toBe(false);
    expect(p.states.reply).toBe("idle");
  });
});
