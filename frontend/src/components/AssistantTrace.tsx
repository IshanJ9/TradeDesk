// Owner: voice-live. Trace display only; no execution or approval actions.
import { useState } from "react";
import type { State } from "../lib/store";
import { traceLabel, traceRuns } from "../lib/trace";
import { AssistantPipeline } from "./AssistantPipeline";
import { Chip, Empty } from "./ui";

export function AssistantTrace({ state }: { state: State }) {
  const runs = traceRuns(state.trace);
  const [expanded, setExpanded] = useState<{ newest: string; ids: Set<string> }>({ newest: "", ids: new Set() });
  const newest = runs[0]?.id ?? "";
  // A newly arriving run expands automatically and resets older runs to collapsed.
  const openIds = expanded.newest === newest ? expanded.ids : new Set([newest]);
  if (!state.trace.length)
    return <Empty title="No assistant activity yet.">Each step the assistant takes (guards, routing, tools) appears here live.</Empty>;
  return (
    <div className="space-y-2">
      {runs[0] && <AssistantPipeline run={runs[0]} />}
      <h3 className="pt-2 text-[13px] font-semibold text-ink">Step log</h3>
      <p className="text-xs text-muted">Live assistant steps. Recorded time adds the reported step durations; it is not wall-clock time. Older steps may leave this session view.</p>
      {runs.map((run) => {
        const open = openIds.has(run.id);
        const blocked = run.steps.some((step) => step.status === "blocked" || step.status === "error");
        return <section key={run.id} className="rounded-lg border border-line">
          <button type="button" aria-expanded={open} aria-controls={`trace-${run.firstSeq}`}
            className="flex min-h-[44px] w-full flex-wrap items-center gap-2 px-3 py-2 text-left text-sm text-ink hover:bg-surface2"
            onClick={() => {
              const ids = new Set(openIds);
              if (open) ids.delete(run.id); else ids.add(run.id);
              setExpanded({ newest, ids });
            }}>
            <span aria-hidden="true">{open ? "▾" : "▸"}</span>
            <span className="min-w-0 break-all">Run {run.id}</span>
            {run.demo && <Chip tone="info">DEMO DATA</Chip>}
            {blocked && <Chip tone="warn">Needs attention</Chip>}
            <span className="text-xs text-muted">{run.steps.length} steps · Recorded time {run.totalMs} ms</span>
          </button>
          <ol id={`trace-${run.firstSeq}`} hidden={!open} className="space-y-2 border-t border-line p-3">
            {run.steps.map((step) => <li key={step.seq} className={`rounded-lg p-2 text-[13px] ${step.status === "blocked" || step.status === "error" ? "border border-[var(--warn-line)] bg-[var(--warn-bg)] text-[var(--warn-ink)]" : "text-ink"}`}>
              <div className="flex flex-wrap items-center gap-2">
                <Chip tone={step.status === "blocked" || step.status === "error" ? "warn" : "plain"}>{step.status}</Chip>
                <span className="min-w-0 break-all">{traceLabel(step.node)}</span>
                <span className="num text-xs">{step.ms == null ? "Time not reported" : `${step.ms} ms`}</span>
              </div>
              {step.detail && <p className="mt-1 whitespace-pre-wrap break-words">{step.detail}</p>}
            </li>)}
          </ol>
        </section>;
      })}
    </div>
  );
}
