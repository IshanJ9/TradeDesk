import type { TraceEvent } from "./types";

export interface TraceStep {
  seq: number; node: string; kind: TraceEvent["kind"];
  status: "running" | "done" | "blocked" | "error";
  detail: string; ms: number | null;
}
export interface TraceRun { id: string; firstSeq: number; demo: boolean; steps: TraceStep[]; totalMs: number; }

export function traceLabel(node: string): string {
  if (node === "input_guard") return "Checked your message";
  if (node === "router") return "Decided what you're asking";
  if (node === "output_guard") return "Checked the reply";
  if (node.startsWith("tool:")) return `Looked up ${node.slice(5).replaceAll("_", " ")}`;
  return node;
}

/** Pair each completion with its most recent unmatched start, not every node with one row.
 * Repeated tool invocations stay separate. Missing starts (bounded event history) are allowed.
 */
export function traceRuns(events: TraceEvent[]): TraceRun[] {
  const runs = new Map<string, TraceRun>();
  const seen = new Set<number>();
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    if (seen.has(event.seq)) continue;
    seen.add(event.seq);
    let run = runs.get(event.run_id);
    if (!run) {
      run = { id: event.run_id, firstSeq: event.seq, demo: event.run_id.startsWith("demo-"), steps: [], totalMs: 0 };
      runs.set(run.id, run);
    }
    const active = event.status !== "start"
      ? [...run.steps].reverse().find((step) => step.node === event.node && step.kind === event.kind && step.status === "running")
      : undefined;
    const status = event.status === "start" ? "running" : event.status === "end" ? "done" : event.status;
    const ms = event.ms != null && Number.isFinite(event.ms) && event.ms >= 0 ? event.ms : null;
    if (active) {
      active.status = status;
      active.detail = event.detail || active.detail;
      active.ms = ms;
    } else {
      run.steps.push({ seq: event.seq, node: event.node, kind: event.kind, status, detail: event.detail, ms });
    }
  }
  for (const run of runs.values()) run.totalMs = run.steps.reduce((sum, step) => sum + (step.ms ?? 0), 0);
  return [...runs.values()].sort((a, b) => b.firstSeq - a.firstSeq);
}
