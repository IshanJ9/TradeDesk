// Owner: risk-goals. Today vs the trader's own limits, goals and "was it worth it". Placeholder.
import type { State } from "../lib/store";
import { Empty } from "./ui";

export function DisciplinePanel({ state }: { state: State }) {
  const d = state.discipline;
  if (!d) return <Empty title="No risk profile yet.">Set your own limits and goal to see today against your usual day.</Empty>;
  return (
    <div className="text-[13px] text-ink">
      <p className="num">
        Orders today {d.orders_today}
        {d.order_limit != null && <> of your limit {d.order_limit}</>}
      </p>
      {d.risk_score != null && (
        <p className="num">
          Risk today {d.risk_score}
          {d.average_score != null && <> &middot; your usual {d.average_score}</>}
        </p>
      )}
      {d.warnings.map((w) => (
        <p key={w} className="text-xs text-muted">{w}</p>
      ))}
    </div>
  );
}
