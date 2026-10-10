// Owner: risk-goals. Every reported figure comes from the backend.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { disciplineApi } from "../lib/api";
import { rupees } from "../lib/format";
import type { State } from "../lib/store";
import type { components } from "../lib/types.gen";
import { DisciplineTrend } from "./DisciplineTrend";
import { DisciplineAnalytics } from "./DisciplineAnalytics";
import { GoalForm, Onboarding, ProfileForm } from "./DisciplineForms";
import { Banner, Button, Chip, Empty, Signed } from "./ui";

type Report = components["schemas"]["DisciplineReport"];
function Meter({ label, value }: { label: string; value: number }) {
  const bounded = Math.min(100, Math.max(0, value));
  return <div role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={bounded}
    className="h-2 w-full overflow-hidden rounded-full bg-surface2"><div className="h-full rounded-full bg-ink" style={{ width: `${bounded}%` }} /></div>;
}
function Card({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return <section aria-label={title} className="min-w-0 space-y-4 rounded-xl border border-line bg-surface p-4">
    <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-serif text-xl italic">{title}</h3>{aside}</div>{children}
  </section>;
}

export function HistoryChart({ history }: { history: Report["history"] }) {
  const days = history.days.filter(d => d.risk_score != null);
  if (!days.length) return <Empty title="No scored past days yet">Days are saved as the app runs. Today will join this comparison tomorrow.</Empty>;
  const max = Math.max(100, ...days.map(d => Math.abs(d.pnl_after_charges)));
  return <figure className="min-w-0">
    <svg viewBox="0 0 360 205" role="img" aria-label="Past days: risk score on the horizontal axis, P&L after charges on the vertical axis" className="w-full text-muted">
      <line x1="50" x2="340" y1="90" y2="90" stroke="currentColor" strokeDasharray="3 4" />
      <line x1="50" x2="50" y1="15" y2="165" stroke="currentColor" />
      <line x1="50" x2="340" y1="165" y2="165" stroke="currentColor" />
      <text x="3" y="22" fill="currentColor" fontSize="10">Gain</text><text x="3" y="94" fill="currentColor" fontSize="10">₹0</text><text x="3" y="161" fill="currentColor" fontSize="10">Loss</text>
      <text x="50" y="181" fill="currentColor" fontSize="10">0</text><text x="320" y="181" fill="currentColor" fontSize="10">100</text>
      <text x="135" y="201" fill="currentColor" fontSize="11">Risk score</text>
      {days.map(d => <circle key={d.day} cx={50 + (d.risk_score ?? 0) * 2.9} cy={90 - d.pnl_after_charges / max * 68}
        r="4" fill={d.demo ? "var(--surface)" : "currentColor"} stroke="currentColor" strokeWidth="1.5" strokeDasharray={d.demo ? "2 1" : undefined}>
        <title>{d.day}: risk {d.risk_score}, net {rupees(d.pnl_after_charges)}{d.demo ? " — DEMO DATA" : ""}</title>
      </circle>)}
    </svg>
    <figcaption className="text-xs text-muted">{history.source === "demo" ? "Hollow dots: DEMO DATA." : "Each dot is one recorded day."} P&L is after charges. Exact values appear below.</figcaption>
  </figure>;
}

export function DisciplinePanel({ state }: { state: State }) {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [editProfile, setEditProfile] = useState(false);
  const [editGoal, setEditGoal] = useState(false);
  const [removeGoal, setRemoveGoal] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const generation = useRef(0);
  const refresh = useCallback(async () => {
    const id = ++generation.current;
    const result = await disciplineApi.report();
    if (id !== generation.current) return;
    setLoading(false);
    if (result.ok) { setReport(result.data); setError(""); }
    else setError(result.status === 503 ? "The broker is unavailable. This report could not be refreshed." : result.message);
  }, []);
  useEffect(() => { void refresh(); return () => { generation.current++; }; }, [refresh, state.discipline]);
  async function deleteGoal() {
    setDeleting(true); const result = await disciplineApi.deleteGoal(); setDeleting(false);
    if (result.ok) { setRemoveGoal(false); await refresh(); } else setError(result.message);
  }
  const g = report?.goal;
  const hidden = report?.profile?.hide_day_pnl ?? false;
  return <div className="min-w-0 space-y-4 pb-4 text-sm text-ink">
    {error && <Banner tone="error" role="alert">{error} {report && "Showing the last loaded report."} <Button onClick={() => void refresh()}>Retry</Button></Banner>}
    {loading && <p role="status" className="text-muted">Loading discipline report…</p>}
    {report && <>
      {!report.profile ? <Card title="Your trading boundaries"><Onboarding saved={() => void refresh()} /></Card> : <>
        <Card title="Today, against your limits" aside={<Button onClick={() => setEditProfile(!editProfile)}>{editProfile ? "Close settings" : "Edit profile"}</Button>}>
          <div className="grid grid-cols-2 gap-4">
            <div><p className="text-xs text-muted">Orders / your limit</p><p className="num text-2xl">{report.today.orders_today} <span className="text-base text-muted">/ {report.profile.max_orders_per_day}</span></p></div>
            <div><p className="text-xs text-muted">P&L after charges · estimate</p><p className="mt-1 break-words text-lg">{hidden ? "Hidden · mindful mode" : <Signed value={report.today.pnl_after_charges}>{rupees(report.today.pnl_after_charges)}</Signed>}</p></div>
            <div><p className="text-xs text-muted">Risk today / 100</p><p className="num text-4xl">{report.score?.total ?? "—"}</p></div>
            <div><p className="text-xs text-muted">{report.history.source === "demo" ? "Demo average" : "Your usual risk"}</p><p className="num text-4xl">{report.history.average_score?.toFixed(1) ?? "—"}</p><p className="text-xs text-muted">{report.history.baseline_days} past scored days</p></div>
          </div>
          <p className="text-xs text-muted">A higher score means more activity or exposure relative to your settings. It does not predict a loss.</p>
          <div className="space-y-3">{report.score?.components.map(c => <div key={c.key}>
            <div className="mb-1 flex justify-between gap-3 text-xs"><span>{c.label} <span className="text-muted">· {c.weight}% weight</span></span><span className="num">{c.score.toFixed(0)}/100</span></div>
            <Meter label={c.label} value={c.score} />
          </div>)}</div>
          {hidden && <p className="text-xs text-muted">Mindful mode hides today’s P&L and the goal values that could reveal it. Recorded past days remain visible.</p>}
        </Card>
        {editProfile && <Card title="Edit your profile"><ProfileForm initial={report.profile} saved={() => { setEditProfile(false); void refresh(); }} cancel={() => setEditProfile(false)} /></Card>}
      </>}
      {report.cooling_off_until && <Banner tone="warn">New orders paused until {new Date(report.cooling_off_until).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })} IST by your cooling-off stop. You can change this in your profile.</Banner>}
      <Card title="Your goal" aside={g && <Button onClick={() => setEditGoal(!editGoal)}>Replace goal</Button>}>
        {editGoal ? <GoalForm today={report.today.day} portfolio={report.today.portfolio_value} saved={() => { setEditGoal(false); void refresh(); }} cancel={() => setEditGoal(false)} /> : g ? <>
          <div className="flex flex-wrap items-center gap-2"><Chip>{g.status}</Chip><span className="text-xs text-muted">Ends {g.goal.end_date} · {g.days_left} days left</span></div>
          {hidden ? <p className="text-muted">Goal progress hidden in mindful mode.</p> : <>
            <p className="num text-lg">{rupees(g.progress_paise)} <span className="text-xs text-muted">of {rupees(g.target_paise)} target gain · {g.progress_pct.toFixed(1)}%</span></p>
            <Meter label="Goal progress" value={g.progress_pct} />
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2"><p>Still needed per week<br /><span className="num font-medium">{g.needed_per_week_paise == null ? "Deadline reached" : rupees(g.needed_per_week_paise)}</span></p>
              <p>Maximum-loss headroom<br /><span className="num font-medium">{rupees(g.loss_headroom_paise)}</span></p></div>
            <p className="text-sm">{g.pace_text}</p>
          </>}
          <p className="text-xs text-muted">{g.note}</p>
          {removeGoal ? <div className="space-y-2"><p>Remove this goal and its starting value?</p><Button disabled={deleting} onClick={() => void deleteGoal()}>Remove goal</Button> <Button disabled={deleting} onClick={() => setRemoveGoal(false)}>Keep goal</Button></div> : <Button variant="plain" onClick={() => setRemoveGoal(true)}>Remove goal…</Button>}
        </> : <><p className="text-muted">Set a target gain and maximum loss in your own numbers. Progress is arithmetic, not a forecast.</p><Button onClick={() => setEditGoal(true)}>Set a goal</Button></>}
      </Card>
      <Card title="Was it worth it?" aside={report.history.source === "demo" && <Chip tone="warn">DEMO DATA</Chip>}>
        <p className="text-xs text-muted">{report.history.source === "demo" ? "Synthetic examples, not your trading record." : "Your recorded past days, compared with your usual risk."} The comparison uses up to 30 recorded days.</p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">{([
          ["Above usual risk", report.history.above_usual], ["At or below usual risk", report.history.at_or_below_usual],
        ] as const).map(([label, group]) => <div key={label} className="rounded-lg border border-line p-3"><p className="font-medium">{label}</p>
          <p className="num my-1 text-lg"><Signed value={group.net_pnl}>{rupees(group.net_pnl)}</Signed></p><p className="text-xs text-muted">{group.days} days · {group.profitable_days} profitable after charges</p></div>)}</div>
        <HistoryChart history={report.history} />
        {report.history.days.length > 0 && <details><summary className="cursor-pointer text-xs">View daily figures</summary><div className="mt-2 overflow-x-auto"><table className="w-full text-left text-xs">
          <thead><tr className="border-b border-line"><th className="py-2">Date</th><th>Risk</th><th className="text-right">Net P&L</th></tr></thead>
          <tbody>{report.history.days.map(d => <tr key={d.day} className="border-b border-line"><td className="py-2">{d.day}{d.demo && <span className="block text-[10px] text-muted">DEMO DATA</span>}</td><td className="num">{d.risk_score ?? "Unscored"}</td><td className="num text-right">{rupees(d.pnl_after_charges)}</td></tr>)}</tbody>
        </table></div></details>}
        <p className="text-xs text-muted">{report.history.note}</p>
      </Card>
      <Card title="Risk and reward over time"><DisciplineTrend history={report.history} /></Card>
      {report.analytics && <Card title="Daily risk, returns and trading patterns"><DisciplineAnalytics data={report.analytics} hidden={hidden} /></Card>}
      <Card title="Charges meter"><div className="grid grid-cols-2 gap-4">
        <div><p className="text-xs text-muted">Today · estimated</p><p className="num text-xl">{rupees(report.charges.today_paise)}</p><p className="text-xs text-muted">{report.charges.turnover_pct.toFixed(2)}% of filled turnover</p></div>
        <div><p className="text-xs text-muted">Recorded charges · last 30 calendar days</p><p className="num text-xl">{rupees(report.charges.last_30_days_paise)}</p></div>
      </div>{report.charges.last_30_days_demo_paise > 0 && <p className="text-xs text-muted">DEMO DATA charges: {rupees(report.charges.last_30_days_demo_paise)} — separate from your recorded charges.</p>}</Card>
      <details className="text-xs text-muted"><summary className="cursor-pointer">Calculation notes and limits</summary><ul className="mt-2 space-y-2 pl-4 list-disc">{report.warnings.map((w, i) => <li key={i}>{w}</li>)}<li>History starts when the app records it. Orders placed in the broker’s app count toward today, but this app cannot block them.</li></ul></details>
    </>}
  </div>;
}
