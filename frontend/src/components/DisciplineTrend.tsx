import type { components } from "../lib/types.gen";
import { rupees } from "../lib/format";
import { Empty } from "./ui";

export function DisciplineTrend({ history }: { history: components["schemas"]["HistoryComparison"] }) {
  const points = history.timeline ?? [];
  if (!points.length) return <Empty title="No recorded past days yet">The trend appears after your first recorded day.</Empty>;
  const low = Math.min(0, ...points.map(p => p.cumulative_pnl_paise));
  const high = Math.max(100, ...points.map(p => p.cumulative_pnl_paise));
  const x = (i: number) => points.length === 1 ? 200 : 65 + i * 285 / (points.length - 1);
  const y = (n: number) => 135 - (n - low) / (high - low) * 110;
  return <figure className="min-w-0 space-y-3">
    <p className="text-xs text-muted">{history.source === "demo" ? "DEMO DATA. " : ""}Cumulative P&L after estimated charges across the recorded days shown, starting from zero. Today is excluded. Gaps are unrecorded days, not zero returns.</p>
    <svg viewBox="0 0 370 255" role="img" aria-label="Cumulative net P&L line with daily risk score bars below" className="w-full text-muted">
      <text x="4" y="16" fill="currentColor" fontSize="10">Net P&amp;L (₹)</text>
      <text x="4" y="33" fill="currentColor" fontSize="9">{(high / 100).toLocaleString("en-IN", { maximumFractionDigits: 0 })}</text>
      <text x="4" y="137" fill="currentColor" fontSize="9">{(low / 100).toLocaleString("en-IN", { maximumFractionDigits: 0 })}</text>
      <line x1="60" x2="355" y1={y(0)} y2={y(0)} stroke="currentColor" strokeDasharray="3 3" />
      <polyline points={points.map((p, i) => `${x(i)},${y(p.cumulative_pnl_paise)}`).join(" ")} fill="none" stroke="var(--ink)" strokeWidth="2" />
      <text x="4" y="164" fill="currentColor" fontSize="10">Risk / 100</text>
      <line x1="60" x2="355" y1="225" y2="225" stroke="currentColor" />
      {points.map((p, i) => <g key={p.day}>
        <circle cx={x(i)} cy={y(p.cumulative_pnl_paise)} r="3" fill="currentColor"><title>{p.day}: cumulative {rupees(p.cumulative_pnl_paise)}{p.demo ? " DEMO DATA" : ""}</title></circle>
        {p.risk_score !== null && <rect x={x(i) - 3} y={225 - p.risk_score * .5} width="6" height={Math.max(1, p.risk_score * .5)} fill="currentColor"><title>{p.day}: risk {p.risk_score}/100</title></rect>}
      </g>)}
      <text x="60" y="245" fill="currentColor" fontSize="9">{points[0]?.day}</text>
      <text x="355" y="245" textAnchor="end" fill="currentColor" fontSize="9">{points.at(-1)?.day}</text>
    </svg>
    <details><summary className="cursor-pointer text-xs">View trend figures</summary><div className="overflow-x-auto"><table className="w-full text-left text-xs">
      <thead><tr><th>Date</th><th>Risk / 100</th><th className="text-right">Cumulative net P&amp;L</th></tr></thead>
      <tbody>{points.map(p => <tr key={p.day}><td className="py-2">{p.day}{p.demo ? " (DEMO)" : ""}</td><td>{p.risk_score ?? "Unscored"}</td><td className="text-right">{rupees(p.cumulative_pnl_paise)}</td></tr>)}</tbody>
    </table></div></details>
  </figure>;
}
