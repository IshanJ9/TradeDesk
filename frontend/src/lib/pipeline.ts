// Turns one assistant run (trace events from the LangGraph orchestrator) into the picture drawn by
// AssistantPipeline: which node is waiting, working, done or blocked, how long each took, and which tools ran.
import type { TraceRun } from "./trace";

export type PipeNode = "message" | "input_guard" | "router" | "model" | "tools" | "output_guard" | "reply";
export type PipeState = "idle" | "running" | "done" | "blocked" | "skipped";

export const PIPE: { id: PipeNode; title: string; who: "you" | "ai" | "code" }[] = [
  { id: "message", title: "Message", who: "you" },
  { id: "input_guard", title: "Input guard", who: "code" },
  { id: "router", title: "Router", who: "code" },
  { id: "model", title: "Model", who: "ai" },
  { id: "tools", title: "Tools", who: "code" },
  { id: "output_guard", title: "Output guard", who: "code" },
  { id: "reply", title: "Reply", who: "code" },
];

export interface ToolChip { name: string; state: "running" | "done" | "blocked"; ms: number | null }
export interface Pipeline {
  states: Record<PipeNode, PipeState>;
  ms: Record<PipeNode, number>;
  modelRounds: number;
  tools: ToolChip[];
  route: string | null; // the router's verdict, e.g. "order · one order: may draft an order card for your approval"
  blocked: { node: PipeNode; detail: string } | null;
  finished: boolean;
}

function nodeOf(name: string): PipeNode | null {
  if (name.startsWith("tool:")) return "tools";
  return name === "input_guard" || name === "router" || name === "model" || name === "output_guard" ? name : null;
}

const rank: Record<PipeState, number> = { idle: 0, skipped: 0, done: 1, running: 2, blocked: 3 };

export function pipelineOf(run: TraceRun): Pipeline {
  const states = Object.fromEntries(PIPE.map((p) => [p.id, "idle"])) as Record<PipeNode, PipeState>;
  const ms = Object.fromEntries(PIPE.map((p) => [p.id, 0])) as Record<PipeNode, number>;
  const tools: ToolChip[] = [];
  let modelRounds = 0;
  let route: string | null = null;
  let blocked: Pipeline["blocked"] = null;
  states.message = "done";

  for (const step of run.steps) {
    const node = nodeOf(step.node);
    if (!node) continue;
    const s: PipeState = step.status === "running" ? "running" : step.status === "done" ? "done" : "blocked";
    if (node === "tools" && step.kind === "tool") tools.push({ name: step.node.slice(5).replaceAll("_", " "), state: s === "blocked" ? "blocked" : s === "running" ? "running" : "done", ms: step.ms });
    if (node === "tools" && step.kind === "guard" && s === "blocked") {
      const existing = [...tools].reverse().find((t) => t.name === step.node.slice(5).replaceAll("_", " "));
      if (existing) existing.state = "blocked";
      else tools.push({ name: step.node.slice(5).replaceAll("_", " "), state: "blocked", ms: null });
    }
    if (node === "model" && step.status !== "running") modelRounds += 1;
    if (node === "router" && step.detail) route = step.detail;
    ms[node] += step.ms ?? 0;
    // a node keeps its strongest state: blocked beats running beats done
    if (rank[s] >= rank[states[node]] || states[node] === "running") states[node] = s === "done" && states[node] === "blocked" ? "blocked" : s;
    if (s === "blocked" && !blocked) blocked = { node, detail: step.detail };
  }
  // a blocked tool is refused in code, but the run carries on; only a blocked guard node ends it there
  if (states.tools === "blocked" && tools.some((t) => t.state !== "blocked")) states.tools = "done";
  if (states.tools === "blocked" && blocked?.node === "tools") states.tools = "blocked";

  const outputDone = states.output_guard === "done" || states.output_guard === "blocked";
  const stoppedAtInput = states.input_guard === "blocked";
  const finished = outputDone || stoppedAtInput;
  if (finished) states.reply = "done";
  if (stoppedAtInput) for (const n of ["router", "model", "tools", "output_guard"] as PipeNode[]) states[n] = "skipped";
  return { states, ms, modelRounds, tools, route, blocked, finished };
}
