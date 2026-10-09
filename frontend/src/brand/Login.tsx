// Log in / create account. UI only for now: no account is checked, and nothing typed here is stored or sent.
// Submitting opens the demo desk. The page says so, so nobody mistakes it for real sign-in.
import { useState, type FormEvent } from "react";
import { navigate } from "../lib/router";
import { CardPreview, Link, Logo } from "./parts";
import { ThemeToggle } from "../components/ThemeToggle";

type Mode = "login" | "signup";

function Field({ id, label, children, hint }: { id: string; label: string; children: React.ReactNode; hint?: string }) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-[13px] font-bold">{label}</label>
      {children}
      {hint && <span className="b-muted text-[12px]">{hint}</span>}
    </div>
  );
}

export function Login() {
  const [mode, setMode] = useState<Mode>("login");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const data = new FormData(e.currentTarget);
    const missing = [...data.entries()].some(([k, v]) => k !== "remember" && String(v).trim() === "");
    if (missing) {
      setError("Please fill in every field.");
      return;
    }
    if (mode === "signup" && !data.get("terms")) {
      setError("Please confirm you understand TradeDesk gives no investment advice.");
      return;
    }
    navigate("app"); // nothing is checked or kept: the form is discarded here
  }

  const tab = (m: Mode, label: string) => (
    <button
      type="button"
      role="tab"
      aria-selected={mode === m}
      onClick={() => { setMode(m); setError(null); }}
      className="h-10 flex-1 rounded-[9px] text-[14px]"
      style={mode === m ? { background: "var(--b-paper)", color: "var(--b-ink)", border: 0, boxShadow: "0 1px 2px rgba(0,0,0,.15)" } : { background: "transparent", color: "var(--b-muted)", border: 0 }}
    >
      {label}
    </button>
  );

  return (
    <div className="brand flex min-h-full flex-col">
      <header className="border-b" style={{ borderColor: "var(--b-line)" }}>
        <div className="b-wrap flex min-h-[60px] items-center gap-4">
          <Link to="landing" aria-label="TradeDesk home" className="inline-flex min-h-[44px] items-center" style={{ color: "inherit", textDecoration: "none" }}><Logo /></Link>
          <span className="flex-1" />
          <Link to="how" className="b-link hidden text-sm sm:inline">How it works</Link>
          <ThemeToggle className="b-muted hover:bg-[var(--b-surface-2)]" />
        </div>
      </header>

      <main className="b-wrap grid flex-1 items-center gap-12 py-12 lg:grid-cols-[1fr_440px] lg:py-16">
        <section className="hidden flex-col gap-6 lg:flex" aria-label="About TradeDesk">
          <div className="b-eyebrow">Out of your way. On your side.</div>
          <h1 className="m-0 text-[48px] font-normal leading-[1.08]">The AI drafts. <i>You decide.</i></h1>
          <ul className="b-muted m-0 flex max-w-[460px] flex-col gap-2.5 pl-[18px] text-[15px]">
            <li>Nothing is sent without your click on one exact card.</li>
            <li>Your own limits, never advice or predictions.</li>
            <li>Works with your 021 Trade account.</li>
          </ul>
          <div className="max-w-[380px]"><CardPreview compact /></div>
        </section>

        <section className="b-panel b-rise mx-auto w-full max-w-[440px] p-6 md:p-8" aria-labelledby="auth-title">
          <h2 id="auth-title" className="m-0 text-[28px] font-normal">{mode === "login" ? "Welcome back" : "Create your account"}</h2>
          <p className="b-muted mb-0 mt-1.5 text-[14px]">{mode === "login" ? "Log in to open your desk." : "It takes a minute. You'll set your own limits next."}</p>

          <div role="tablist" aria-label="Log in or create an account" className="mt-5 flex gap-1 rounded-[11px] p-1" style={{ background: "var(--b-surface-3)" }}>
            {tab("login", "Log in")}
            {tab("signup", "Create account")}
          </div>

          <div role="note" className="mt-5 rounded-[10px] px-3.5 py-2.5 text-[13px]" style={{ background: "var(--b-tag-ai)", color: "var(--b-ink)" }}>
            <strong className="font-bold">Preview:</strong> sign-in isn't connected yet. Any details open the demo desk; nothing you type is checked, stored or sent.
          </div>

          <form className="mt-5 flex flex-col gap-4" onSubmit={submit} noValidate>
            {mode === "signup" && (
              <Field id="name" label="Your name">
                <input id="name" name="name" className="b-input" autoComplete="name" />
              </Field>
            )}
            <Field id="email" label={mode === "login" ? "Email or 021 client ID" : "Email"}>
              <input id="email" name="email" className="b-input" type={mode === "login" ? "text" : "email"} autoComplete={mode === "login" ? "username" : "email"} />
            </Field>
            <Field id="password" label="Password" hint={mode === "signup" ? "At least 8 characters." : undefined}>
              <div className="relative">
                <input id="password" name="password" className="b-input pr-20" type={showPassword ? "text" : "password"} autoComplete={mode === "login" ? "current-password" : "new-password"} />
                <button type="button" onClick={() => setShowPassword((v) => !v)} aria-pressed={showPassword} className="b-link absolute right-2 top-1/2 h-9 -translate-y-1/2 rounded-md px-2.5 text-[13px]" style={{ background: "none", border: 0 }}>
                  {showPassword ? "Hide" : "Show"}
                </button>
              </div>
            </Field>

            {mode === "login" ? (
              <div className="flex flex-wrap items-center justify-between gap-2 text-[13px]">
                <label className="flex min-h-[40px] items-center gap-2.5"><input type="checkbox" name="remember" className="h-[18px] w-[18px]" style={{ accentColor: "var(--b-violet)" }} />Keep me signed in</label>
                <span className="b-muted" title="Not available in the preview">Forgot password?</span>
              </div>
            ) : (
              <label className="flex items-start gap-2.5 text-[13px]"><input type="checkbox" name="terms" className="mt-0.5 h-[18px] w-[18px] flex-none" style={{ accentColor: "var(--b-violet)" }} />I understand TradeDesk shows facts and my own limits, and never gives investment advice.</label>
            )}

            {error && <div role="alert" className="rounded-[10px] px-3.5 py-2.5 text-[13px]" style={{ background: "var(--b-tag-never)", color: "var(--b-loss)" }}>{error}</div>}

            <button type="submit" className="b-btn b-btn-primary w-full">{mode === "login" ? "Log in" : "Create account"}</button>
          </form>

          <p className="b-muted mb-0 mt-5 text-center text-[13px]">
            {mode === "login" ? "New to TradeDesk? " : "Already have an account? "}
            <button type="button" className="b-link min-h-[44px]" style={{ background: "none", border: 0, padding: "0 4px", font: "inherit" }} onClick={() => { setMode(mode === "login" ? "signup" : "login"); setError(null); }}>
              {mode === "login" ? "Create an account" : "Log in"}
            </button>
          </p>
        </section>
      </main>
    </div>
  );
}
