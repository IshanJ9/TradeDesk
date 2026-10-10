import { describe, expect, it } from "vitest";
import { APPROVAL, NODES, ROUTES, SCENARIOS, nodeStates } from "./graph";

const order = SCENARIOS.find((s) => s.id === "order")!;
const trick = SCENARIOS.find((s) => s.id === "trick")!;

describe("the how-it-works graph", () => {
  it("every scenario step names a real node", () => {
    const ids = new Set([...NODES, ...APPROVAL].map((n) => n.id));
    for (const s of SCENARIOS) for (const step of s.steps) expect(ids.has(step.node)).toBe(true);
  });

  it("only the order scenario reaches the broker, and only after you approve", () => {
    for (const s of SCENARIOS) {
      const nodes = s.steps.map((x) => x.node);
      if (s.id === "order") expect(nodes.indexOf("approve")).toBeLessThan(nodes.indexOf("broker"));
      else expect(nodes).not.toContain("broker");
    }
  });

  it("names the route for every message that reaches the router, and none for one stopped before it", () => {
    expect(ROUTES.map((r) => r.id)).toEqual(["read", "risk", "order", "rule", "plan"]);
    for (const s of SCENARIOS) {
      const routed = s.steps.some((x) => x.node === "router");
      if (routed) expect(ROUTES.some((r) => r.id === s.route)).toBe(true);
      else expect(s.route).toBeNull();
    }
  });

  it("marks the current step active, earlier ones done, and a block as blocked", () => {
    expect(nodeStates(order, 3).router).toBe("active");
    expect(nodeStates(order, 3).input_guard).toBe("done");
    expect(nodeStates(order, 3).model).toBe("idle");
    const blocked = nodeStates(trick, trick.steps.length);
    expect(blocked.input_guard).toBe("blocked");
    expect(blocked.model).toBe("skipped");
  });
});
