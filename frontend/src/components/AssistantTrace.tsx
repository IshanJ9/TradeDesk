// Owner: voice-live. Live view of the assistant's steps (TraceEvent on /ws). Placeholder until that branch lands.
import type { State } from "../lib/store";
import { Chip, Empty } from "./ui";

export function AssistantTrace({ state }: { state: State }) {
  if (!state.trace.length)
    return <Empty title="No assistant activity yet.">Each step the assistant takes (guards, routing, tools) appears here live.</Empty>;
  return (
    <ul>
      {state.trace.map((e) => (
        <li key={e.seq} className="flex items-center gap-2 border-b border-line py-1.5 text-[13px] last:border-b-0">
          <Chip tone={e.status === "blocked" || e.status === "error" ? "warn" : "plain"}>{e.status}</Chip>
          <span className="num text-ink">{e.node}</span>
          {e.ms != null && <span className="num text-xs text-muted">{e.ms} ms</span>}
          {e.detail && <span className="min-w-0 truncate text-xs text-muted">{e.detail}</span>}
        </li>
      ))}
    </ul>
  );
}
