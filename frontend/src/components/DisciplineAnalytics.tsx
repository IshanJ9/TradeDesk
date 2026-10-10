import type { components } from "../lib/types.gen";
import { rupees } from "../lib/format";
import { Empty } from "./ui";

type Analytics = components["schemas"]["AnalyticsReport"];
const number = (v: number | null | undefined, suffix = "") => v == null ? "Not enough data" : `${v.toFixed(2)}${suffix}`;

export function DisciplineAnalytics({ data, hidden }: { data: Analytics; hidden: boolean }) {
  const summary = data.summary;
  const max = Math.max(100, ...data.days.map(d => Math.abs(d.pnl_after_charges)));
  const x = (i: number) => data.days.length === 1 ? 190 : 55 + i * 285 / (data.days.length - 1);
  return <div className="min-w-0 space-y-5">
    <p className="text-xs text-muted">Recorded activity only. Missing history is left empty. Past patterns do not predict future performance.</p>
    {data.today && <div className="rounded-lg border border-line p-3 text-sm">
      <p className="font-medium">Today · partial observations</p>
      <p>Average observed risk: {number(data.today.average_risk_score)} / 100 · {data.today.risk_samples} scored samples</p>
      <p>Intraday share of filled turnover: {number(data.today.intraday_share_pct, "%")}. Current losing streak: {data.today.consecutive_losses ?? "Not recorded"}.</p>
      {!hidden && <p>Return since first snapshot: {number(data.today.observed_return_pct, "%")}</p>}
      <p className="text-xs text-muted">{data.today.first_observed_at ? `First snapshot ${new Date(data.today.first_observed_at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })} IST` : "No baseline yet"}. One sample per five minutes.</p>
    </div>}
    <div className="rounded-lg border border-line p-3 text-sm">
      <p className="font-medium">Your trading pace</p>
      <p>{data.pace.orders_now ?? "Unavailable:"} orders in the last {data.pace.window_minutes} minutes. Usual in the same clock window: {number(data.pace.usual_orders)} ({data.pace.baseline_days} covered past days).</p>
      <p className="mt-1 text-xs text-muted">{data.pace.note}</p>
    </div>
    {!summary.days ? <Empty title="No recorded past days yet">Your 021 activity will build this view as the app records it. Today's observations enter past-day comparisons tomorrow.</Empty> : <>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div><p className="text-xs text-muted">Average daily observed risk / 100</p><p className="num text-xl">{number(summary.average_risk)}</p><p className="text-xs text-muted">{summary.risk_days} days with observations</p></div>
        <div><p className="text-xs text-muted">Average daily net P&amp;L · estimated</p><p className="num text-xl">{summary.average_net_pnl_paise == null ? "—" : rupees(summary.average_net_pnl_paise)}</p><p className="text-xs text-muted">{summary.days} recorded past days</p></div>
        <div><p className="text-xs text-muted">Average observed-period return</p><p className="num text-xl">{number(summary.average_observed_return_pct, "%")}</p><p className="text-xs text-muted">{summary.return_days} days with a baseline; periods may be partial days</p></div>
        <div><p className="text-xs text-muted">Profitable recorded days</p><p className="num text-xl">{summary.profitable_days} / {summary.days} ({number(summary.profitable_day_pct, "%")})</p></div>
      </div>
      <figure>
        <svg viewBox="0 0 365 265" role="img" aria-label="Daily net P&L line and average observed risk bars" className="w-full text-muted">
          <text x="4" y="15" fill="currentColor" fontSize="10">Daily net P&amp;L · gain / loss</text>
          <line x1="50" x2="345" y1="85" y2="85" stroke="currentColor" strokeDasharray="3 3" />
          <text x="4" y="88" fill="currentColor" fontSize="10">₹0</text>
          <polyline points={data.days.map((d, i) => `${x(i)},${85 - d.pnl_after_charges / max * 55}`).join(" ")} fill="none" stroke="var(--ink)" strokeWidth="2" />
          <text x="4" y="165" fill="currentColor" fontSize="10">Average risk / 100</text>
          {data.days.map((d, i) => <g key={d.day}>
            <circle cx={x(i)} cy={85 - d.pnl_after_charges / max * 55} r="3" fill="currentColor"><title>{d.day}: {rupees(d.pnl_after_charges)}</title></circle>
            {d.average_risk_score != null && <rect x={x(i) - 3} y={235 - d.average_risk_score / 2} width="6" height={Math.max(1, d.average_risk_score / 2)} fill="currentColor"><title>{d.day}: average risk {d.average_risk_score.toFixed(2)}</title></rect>}
          </g>)}
          <text x="50" y="255" fill="currentColor" fontSize="9">{data.days[0]?.day}</text><text x="345" y="255" textAnchor="end" fill="currentColor" fontSize="9">{data.days.at(-1)?.day}</text>
        </svg>
        <figcaption className="text-xs text-muted">Missing risk observations have no bar. Exact values and coverage are in the table.</figcaption>
      </figure>
      <details><summary className="cursor-pointer text-sm">Daily figures and coverage</summary><div className="overflow-x-auto"><table className="w-full text-left text-xs">
        <thead><tr><th>Date</th><th>Avg risk</th><th>Last risk</th><th>Samples</th><th>Net P&amp;L</th><th>Observed return</th><th>First / last observation (IST)</th></tr></thead>
        <tbody>{data.days.map(d => <tr key={d.day} className="border-b border-line"><td className="py-2">{d.day}</td><td>{number(d.average_risk_score)}</td><td>{d.risk_score ?? "Unscored"}</td><td>{d.risk_samples}</td><td>{rupees(d.pnl_after_charges)}</td><td>{number(d.observed_return_pct, "%")}</td><td>{d.first_observed_at && d.last_observed_at ? `${new Date(d.first_observed_at).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata" })} / ${new Date(d.last_observed_at).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata" })}` : "Not recorded"}</td></tr>)}</tbody>
      </table></div></details>
      {[['By weekday', data.weekdays], ['By average observed risk band', data.risk_bands]] .map(([label, rows]) => <details key={String(label)}><summary className="cursor-pointer text-sm">{String(label)}</summary><div className="overflow-x-auto"><table className="w-full text-left text-xs">
        <thead><tr><th>Group</th><th>Days</th><th>Avg risk</th><th>Avg net P&amp;L</th><th>Avg observed return</th><th>Return days</th></tr></thead>
        <tbody>{(rows as Analytics['weekdays']).map(g => <tr key={g.label} className="border-b border-line"><td className="py-2">{g.label}</td><td>{g.days}</td><td>{number(g.average_risk)}</td><td>{g.average_net_pnl_paise == null ? "—" : rupees(g.average_net_pnl_paise)}</td><td>{number(g.average_observed_return_pct, "%")}</td><td>{g.return_days}</td></tr>)}</tbody>
      </table></div></details>)}
      <div className="text-sm"><p>Observed re-entries after losses: {data.reentries}. Cooling-off breaches: {data.cooling_off_breaches}. Coverage: {data.behavior_days} past days.</p></div>
      <details><summary className="cursor-pointer text-sm">Daily behavior patterns</summary><div className="overflow-x-auto"><table className="w-full text-left text-xs">
        <thead><tr><th>Date</th><th>Intraday turnover share</th><th>Last losing streak</th><th>Re-entries</th><th>Cooling breaches</th></tr></thead>
        <tbody>{data.days.map(d => <tr key={d.day} className="border-b border-line"><td className="py-2">{d.day}</td><td>{number(d.intraday_share_pct, "%")}</td><td>{d.consecutive_losses ?? "Not recorded"}</td><td>{d.reentries ?? "Not recorded"}</td><td>{d.cooling_off_breaches ?? "Not recorded"}</td></tr>)}</tbody>
      </table></div></details>
      {[['Most-traded symbols', data.symbols], ['Order timing (IST)', data.hours]].map(([label, rows]) => <details key={String(label)}><summary className="cursor-pointer text-sm">{String(label)}</summary><p className="text-xs text-muted">Order records available on {data.pattern_days} past days. All sources included.</p><div className="overflow-x-auto"><table className="w-full text-left text-xs">
        <thead><tr><th>Group</th><th>Orders</th><th>External</th><th>Filled turnover</th></tr></thead><tbody>{(rows as Analytics['symbols']).map(r => <tr key={r.label}><td className="py-2">{r.label}</td><td>{r.orders}</td><td>{r.external_orders}</td><td>{rupees(r.filled_turnover_paise)}</td></tr>)}</tbody>
      </table></div></details>)}
    </>}
    <details><summary className="cursor-pointer text-xs">How these figures are calculated</summary><ul className="mt-2 list-disc space-y-2 pl-4 text-xs text-muted">{data.notes.map(n => <li key={n}>{n}</li>)}</ul></details>
  </div>;
}
