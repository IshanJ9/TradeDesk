// The assistant's latest run, drawn: the real LangGraph nodes light up as the backend reports them. Display only.
// `compact` is the slim strip shown in the chat while the assistant works; the full version leads the Assistant tab.
import { Fragment } from "react";
import { PIPE, pipelineOf, type PipeState } from "../lib/pipeline";
import type { TraceRun } from "../lib/trace";

const WHO = { you: "YOU", ai: "AI", code: "CODE" } as const;
const WHO_COLOR = { you: "var(--muted)", ai: "var(--info-ink)", code: "var(--gain)" } as const;
const lit = (s: PipeState) => s === "done" || s === "running" || s === "blocked";

export function AssistantPipeline({ run, compact = false }: { run: TraceRun; compact?: boolean }) {
  const p = pipelineOf(run);
  const total = Object.values(p.ms).reduce((a, b) => a + b, 0);
  const working = !p.finished;

  return (
    <div className={compact ? "rounded-xl border border-[var(--info-line)] bg-[var(--info-bg)] p-3" : "rounded-2xl border border-line bg-surface p-4 shadow-[var(--shadow)]"} aria-label="What the assistant is doing">
      <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
        <span className="h-2 w-2 rounded-full" style={{ background: p.blocked ? "var(--loss)" : working ? "var(--violet)" : "var(--gain)", boxShadow: working ? "0 0 10px var(--violet)" : undefined }} aria-hidden="true" />
        <span className="font-semibold text-ink" role="status">{working ? "Working…" : p.blocked ? "Code stopped something" : "Done"}</span>
        {run.demo && <span className="rounded border border-line px-1.5 text-[10px] text-muted">DEMO DATA</span>}
        <span className="flex-1" />
        <span className="num text-muted">{total} ms</span>
      </div>

      <ol className="m-0 flex list-none items-stretch p-0" aria-label="Steps">
        {PIPE.map((n, i) => {
          const s = p.states[n.id];
          return (
            <Fragment key={n.id}>
              {i > 0 && <li aria-hidden="true" className="pl-edge" data-lit={lit(p.states[PIPE[i - 1]!.id]) && lit(s)} />}
              <li className="pl-node flex-1" data-state={s} aria-current={s === "running" ? "step" : undefined} title={`${n.title}: ${s}`}>
                {!compact && <span className="pl-who" style={{ color: s === "blocked" ? "var(--loss)" : WHO_COLOR[n.who] }}>{s === "blocked" ? "BLOCKED" : WHO[n.who]}</span>}
                <div className={`truncate font-semibold text-ink ${compact ? "text-[10.5px]" : "mt-0.5 text-[12.5px]"}`}>{n.title}</div>
                {!compact && <div className="num text-[11px] text-muted">{s === "skipped" ? "skipped" : p.ms[n.id] ? `${p.ms[n.id]} ms` : s === "running" ? "…" : " "}</div>}
              </li>
            </Fragment>
          );
        })}
      </ol>

      {!compact && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5 text-xs">
          {p.route && <span className="rounded-full border border-line px-2.5 py-1 text-muted">Router: {p.route}</span>}
          {p.modelRounds > 0 && <span className="rounded-full border border-line px-2.5 py-1 text-muted">Model rounds: <span className="num">{p.modelRounds}</span> of 6 max</span>}
          {p.tools.map((t, i) => (
            <span key={`${t.name}-${i}`} className="rounded-full px-2.5 py-1" style={t.state === "blocked"
              ? { background: "var(--error-bg)", color: "var(--error-ink)", border: "1px solid var(--error-line)" }
              : { background: "var(--info-bg)", color: "var(--info-ink)", border: "1px solid var(--info-line)" }}>
              {t.state === "blocked" ? "Refused: " : ""}{t.name}{t.ms != null ? <span className="num"> · {t.ms} ms</span> : null}
            </span>
          ))}
        </div>
      )}

      {p.blocked && (
        <p className={`mb-0 rounded-lg border border-[var(--error-line)] bg-[var(--error-bg)] px-3 py-2 text-[var(--error-ink)] ${compact ? "mt-2 text-[11.5px]" : "mt-3 text-[13px]"}`}>
          <strong>{PIPE.find((n) => n.id === p.blocked!.node)?.title}:</strong> {p.blocked.detail || "stopped by code"}
        </p>
      )}
    </div>
  );
}
