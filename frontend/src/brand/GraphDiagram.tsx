// The LangGraph orchestrator, drawn. Pick an example message and watch it travel through the real nodes; the
// log beside it says what each node did. Plays step by step; with reduced motion it shows the whole path at once.
import { Fragment, useEffect, useState } from "react";
import { APPROVAL, NODES, ROUTES, SCENARIOS, nodeStates, type GraphNode, type NodeId, type NodeState, type RouteId } from "./graph";

const STEP_MS = 1100;
const WHO_LABEL = { you: "YOU", ai: "AI", code: "CODE" } as const;

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}

function Node({ node, state }: { node: GraphNode; state: NodeState }) {
  return (
    <div className="g-node min-w-0 lg:flex-1" data-state={state} aria-current={state === "active" ? "step" : undefined}>
      <div className="flex items-center gap-2">
        <span className="b-tag" data-who={state === "blocked" ? "never" : node.who}>{state === "blocked" ? "BLOCKED" : WHO_LABEL[node.who]}</span>
      </div>
      <div className="mt-2 text-[14.5px] font-bold leading-tight">{node.title}</div>
      <div className="b-muted mt-1 text-[12.5px] leading-snug">{node.role}</div>
    </div>
  );
}

const lit = (s: NodeState) => s === "done" || s === "active" || s === "blocked";

function Row({ nodes, states }: { nodes: GraphNode[]; states: Record<NodeId, NodeState> }) {
  return (
    <div className="flex flex-col items-stretch lg:flex-row lg:items-stretch">
      {nodes.map((n, i) => (
        <Fragment key={n.id}>
          {i > 0 && <span className="g-edge" data-lit={lit(states[nodes[i - 1]!.id]) && lit(states[n.id])} aria-hidden="true" />}
          <Node node={n} state={states[n.id]} />
        </Fragment>
      ))}
    </div>
  );
}

// The router's five routes; the one this message took lights up once the router has run.
function Routes({ chosen }: { chosen: RouteId | null }) {
  return (
    <div className="mt-3 flex flex-wrap items-center gap-2" aria-label="The router's five routes">
      <span className="b-muted text-[12.5px]">Routes:</span>
      {ROUTES.map((r) => {
        const on = r.id === chosen;
        return (
          <span
            key={r.id}
            title={`${r.title}: ${r.allows}`}
            aria-current={on ? "true" : undefined}
            className="rounded-full px-3 py-1 text-[12.5px]"
            style={on
              ? { background: "var(--b-ink)", color: "var(--b-ink-inverse)", fontWeight: 700 }
              : { color: "var(--b-muted)", boxShadow: "inset 0 0 0 1px var(--b-line-strong)" }}
          >
            {r.title}
            {on && <span className="font-normal"> · {r.allows}</span>}
          </span>
        );
      })}
    </div>
  );
}

export function GraphDiagram() {
  const [scenarioId, setScenarioId] = useState(SCENARIOS[1]!.id);
  const [shown, setShown] = useState(0);
  const [run, setRun] = useState(0); // bumps to replay
  const scenario = SCENARIOS.find((s) => s.id === scenarioId)!;
  const total = scenario.steps.length;

  useEffect(() => {
    if (prefersReducedMotion()) {
      setShown(total);
      return;
    }
    setShown(1);
    const t = setInterval(() => setShown((n) => (n >= total ? n : n + 1)), STEP_MS);
    return () => clearInterval(t);
  }, [scenarioId, run, total]);

  const states = nodeStates(scenario, shown);
  const usesApproval = scenario.steps.some((s) => s.node === "approve");
  const done = shown >= total;

  return (
    <div className="b-panel p-5 md:p-7">
      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Example messages">
        {SCENARIOS.map((s) => (
          <button
            key={s.id}
            type="button"
            onClick={() => { setScenarioId(s.id); setRun((r) => r + 1); }}
            aria-pressed={s.id === scenarioId}
            className="min-h-[38px] rounded-[10px] px-3.5 text-[13px]"
            style={s.id === scenarioId
              ? { background: "var(--b-ink)", color: "var(--b-ink-inverse)", border: 0 }
              : { background: "transparent", color: "var(--b-ink)", border: "1px solid var(--b-line-strong)" }}
          >
            {s.label}
          </button>
        ))}
        <span className="flex-1" />
        <button type="button" onClick={() => setRun((r) => r + 1)} className="b-link min-h-[38px] px-2 text-[13px]" style={{ background: "none", border: 0 }}>
          Replay
        </button>
      </div>

      <div className="mt-5 flex flex-wrap items-center gap-3">
        <span className="b-muted text-[13px]">The message:</span>
        <span className="b-quote">{scenario.message}</span>
      </div>

      <div className="mt-6">
        <div className="b-eyebrow mb-3" style={{ color: "var(--b-muted)" }}>Inside the graph · <span className="n">app/agent/graph.py</span></div>
        <Row nodes={NODES} states={states} />
        <Routes chosen={lit(states.router) && states.router !== "blocked" ? scenario.route : null} />
        <p className="b-muted mb-0 mt-3 text-[12.5px]">Each route allows only its own tools, and code refuses any other. The model and the tools can go back and forth, at most 6 rounds, before the output guard. The router picks tools only: it never decides a price or a quantity.</p>
      </div>

      <div className="mt-6 border-t pt-5" style={{ borderColor: "var(--b-line)", opacity: usesApproval ? 1 : 0.5 }}>
        <div className="b-eyebrow mb-3" style={{ color: "var(--b-muted)" }}>Outside the graph · the only way to the broker</div>
        <Row nodes={APPROVAL} states={states} />
      </div>

      <div className="mt-6 grid gap-4 lg:grid-cols-[1fr_280px]">
        <ol className="m-0 flex list-none flex-col gap-1.5 p-0" aria-live="polite">
          {scenario.steps.slice(0, shown).map((step, i) => {
            const node = [...NODES, ...APPROVAL].find((n) => n.id === step.node)!;
            const blocked = step.status === "blocked";
            return (
              <li key={`${run}-${i}`} className="b-rise flex gap-3 text-[13.5px]">
                <span className="n w-6 flex-none text-right" style={{ color: "var(--b-faint)" }}>{i + 1}</span>
                <span className="w-[112px] flex-none font-bold" style={blocked ? { color: "var(--b-loss)" } : undefined}>{node.title}</span>
                <span className={blocked ? "" : "b-muted"} style={blocked ? { color: "var(--b-loss)" } : undefined}>{step.text}</span>
              </li>
            );
          })}
        </ol>
        <div className="rounded-xl p-4 text-[14px]" style={{ background: done ? "var(--b-tag-ai)" : "var(--b-surface-2)", boxShadow: "inset 0 0 0 1px var(--b-line)", transition: "background 240ms ease" }}>
          <div className="b-eyebrow mb-1.5">{done ? "Result" : "Playing…"}</div>
          {done ? scenario.outcome : `Step ${shown} of ${total}`}
        </div>
      </div>
    </div>
  );
}
