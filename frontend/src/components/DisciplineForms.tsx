import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { disciplineApi } from "../lib/api";
import type { components } from "../lib/types.gen";
import { Banner, Button } from "./ui";

type Schemas = components["schemas"];
export type Profile = Schemas["RiskProfile-Output"];
const input = "mt-1 block w-full min-w-0 rounded-lg border border-strong bg-surface px-3 py-2 text-sm text-ink";
export function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="block min-w-0 text-xs text-muted">{label}{children}</label>;
}

const numbers = [
  ["max_orders_per_day", "Orders per day", 100000, 1],
  ["max_order_pct", "Single order (% of portfolio)", 100, .1],
  ["max_stock_pct", "One stock (% of portfolio)", 100, .1],
  ["daily_loss_limit_pct", "Daily loss (% of portfolio)", 100, .1],
  ["cooling_off_after_losses", "Consecutive losses before cooling off", 100000, 1],
  ["cooling_off_minutes", "Cooling-off window (minutes)", 1440, 1],
  ["reentry_minutes", "Re-entry window (minutes)", 1440, 1],
] as const;

export function ProfileForm({ initial, saved, cancel }: { initial: Profile; saved: () => void; cancel?: () => void }) {
  const [value, setValue] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError("");
    const result = await disciplineApi.saveProfile(value);
    setBusy(false);
    if (result.ok) saved(); else setError(result.message);
  }
  return <form onSubmit={submit} className="space-y-4">
    <p className="text-xs text-muted">Starting values; change them to your own. These are not recommendations.</p>
    <fieldset disabled={busy} className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2">
      {numbers.map(([key, label, max, step]) => <Field label={label} key={key}>
        <input className={input} type="number" required min="1" max={max} step={step}
          value={Number.isNaN(value[key]) ? "" : value[key]}
          onChange={e => setValue({ ...value, style: "custom", [key]: e.target.valueAsNumber })} />
      </Field>)}
    </fieldset>
    <fieldset disabled={busy} className="space-y-3 text-sm">
      {([ ["intraday_allowed", "Allow intraday (MIS) trading in my profile"],
          ["hard_order_limit", "Block new orders above my daily order limit"],
          ["hard_stop_on_daily_loss", "Block new orders when my daily loss limit is reached"],
          ["hide_day_pnl", "Mindful mode: hide today’s P&L in Discipline"] ] as const).map(([key, label]) =>
        <label key={key} className="flex items-start gap-2"><input type="checkbox" checked={value[key]}
          onChange={e => setValue({ ...value, [key]: e.target.checked })} className="mt-1 shrink-0" />{label}</label>)}
    </fieldset>
    <Banner tone="info">Limits normally show warnings. The two “Block new orders” switches also prevent approval.
      Cancellations remain available; modifications receive warnings only. Plan legs and orders sent from the broker’s own app aren’t blocked here.</Banner>
    {error && <Banner tone="error" role="alert">{error}</Banner>}
    <div className="flex flex-wrap gap-2"><Button type="submit" disabled={busy}>{busy ? "Saving…" : "Save profile"}</Button>
      {cancel && <Button type="button" variant="plain" disabled={busy} onClick={cancel}>Cancel</Button>}</div>
  </form>;
}

export function Onboarding({ saved }: { saved: () => void }) {
  const [answers, setAnswers] = useState<Schemas["OnboardingAnswers"]>({ daily_loss_comfort: "small", holding_period: "days",
    usual_orders: "up_to_three", intraday_allowed: false, aim: "steady_progress" });
  const [suggestion, setSuggestion] = useState<Schemas["OnboardingSuggestion"] | null>(null);
  const [choices, setChoices] = useState<Schemas["PresetOption"][]>([]);
  const [chosen, setChosen] = useState<Profile | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { let active = true; void disciplineApi.presets().then(r => { if (active && r.ok) setChoices(r.data); });
    return () => { active = false; }; }, []);
  async function suggest(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError("");
    const result = await disciplineApi.suggest(answers); setBusy(false);
    if (result.ok) { setSuggestion(result.data); setChosen(result.data.profile); } else setError(result.message);
  }
  if (suggestion && chosen) return <div className="space-y-4">
    <Banner tone="info">Suggested starting point: {suggestion.preset}. {suggestion.explanation} Review before saving.</Banner>
    <Field label="Starting preset"><select className={input} value={chosen.style} onChange={e => {
      const match = choices.find(p => p.name === e.target.value); if (match) setChosen(match.profile);
    }}>{choices.length ? choices.map(p => <option key={p.name} value={p.name}>{p.name}</option>) : <option>{chosen.style}</option>}</select></Field>
    <ProfileForm key={JSON.stringify(chosen)} initial={chosen} saved={saved} cancel={() => setSuggestion(null)} />
  </div>;
  const selects = [
    ["daily_loss_comfort", "1. How much daily loss are you comfortable with?", [["small", "Small"], ["moderate", "Moderate"], ["larger", "Larger"]]],
    ["holding_period", "2. How long do you usually hold?", [["weeks_or_more", "Weeks or longer"], ["days", "A few days"], ["same_day", "Within the day"]]],
    ["usual_orders", "3. How many orders do you usually place daily?", [["up_to_three", "Up to three"], ["four_to_six", "Four to six"], ["seven_or_more", "Seven or more"]]],
    ["aim", "5. What is your main aim?", [["preserve_capital", "Preserve capital"], ["steady_progress", "Steady progress"], ["active_trading", "Active trading"]]],
  ] as const;
  return <form onSubmit={suggest} className="space-y-4">
    <p className="text-sm text-muted">Choose your own boundaries. These five answers suggest editable starting values; nothing is saved automatically.</p>
    <fieldset disabled={busy} className="space-y-3">
      {selects.slice(0, 3).map(([key, label, options]) => <Field label={label} key={key}><select className={input}
        value={answers[key]} onChange={e => setAnswers({ ...answers, [key]: e.target.value })}>
        {options.map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></Field>)}
      <Field label="4. Do you want intraday trading?"><select className={input} value={String(answers.intraday_allowed)}
        onChange={e => setAnswers({ ...answers, intraday_allowed: e.target.value === "true" })}><option value="false">No</option><option value="true">Yes</option></select></Field>
      <Field label={selects[3][1]}><select className={input} value={answers.aim} onChange={e => setAnswers({ ...answers, aim: e.target.value as typeof answers.aim })}>
        {selects[3][2].map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></Field>
    </fieldset>
    {error && <Banner tone="error" role="alert">{error}</Banner>}
    <Button disabled={busy}>{busy ? "Preparing…" : "Review starting profile"}</Button>
  </form>;
}

export function GoalForm({ saved, cancel, portfolio, today }: { saved: () => void; cancel: () => void; portfolio: number; today: string }) {
  const [mode, setMode] = useState("rupees");
  const [target, setTarget] = useState("");
  const [loss, setLoss] = useState("");
  const [end, setEnd] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(e: FormEvent) {
    e.preventDefault(); setError("");
    const targetValue = Number(target), maxLoss = Math.round(Number(loss) * 100);
    if (end <= today || maxLoss > portfolio || !Number.isSafeInteger(maxLoss) || maxLoss <= 0 ||
        !Number.isFinite(targetValue) || targetValue <= 0 || (mode === "rupees" && !Number.isSafeInteger(Math.round(targetValue * 100)))) {
      setError("Use a future end date and positive amounts. Maximum loss cannot exceed your current portfolio value."); return;
    }
    setBusy(true);
    const result = await disciplineApi.saveGoal({ end_date: end, max_acceptable_loss_paise: maxLoss,
      ...(mode === "rupees" ? { target_paise: Math.round(targetValue * 100) } : { target_pct: targetValue }) });
    setBusy(false); if (result.ok) saved(); else setError(result.message);
  }
  return <form onSubmit={submit} className="space-y-3">
    <p className="text-xs text-muted">Saving starts a new goal today and records your current portfolio value.</p>
    <fieldset disabled={busy} className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <Field label="Target gain unit"><select className={input} value={mode} onChange={e => setMode(e.target.value)}><option value="rupees">Rupees</option><option value="percent">Percent</option></select></Field>
      <Field label={mode === "rupees" ? "Target gain (₹)" : "Target gain (%)"}><input className={input} required type="number" min={mode === "rupees" ? .01 : 1}
        max={mode === "rupees" ? undefined : 100} step="0.01" value={target} onChange={e => setTarget(e.target.value)} /></Field>
      <Field label="End date"><input className={input} required type="date" min={today} value={end} onChange={e => setEnd(e.target.value)} /></Field>
      <Field label="Maximum acceptable loss (₹)"><input className={input} required type="number" min="0.01" max={portfolio / 100} step="0.01" value={loss} onChange={e => setLoss(e.target.value)} /></Field>
    </fieldset>
    {error && <Banner tone="error" role="alert">{error}</Banner>}
    <div className="flex gap-2"><Button disabled={busy}>{busy ? "Saving…" : "Save goal"}</Button><Button type="button" variant="plain" disabled={busy} onClick={cancel}>Cancel</Button></div>
  </form>;
}
