// Shared parts of the public pages: links, logo, header, footer, and the example order card.
import type { AnchorHTMLAttributes, ReactNode } from "react";
import { ThemeToggle } from "../components/ThemeToggle";
import { navigate, pathOf, type Route } from "../lib/router";

export function Link({ to, children, ...rest }: { to: Route; children: ReactNode } & AnchorHTMLAttributes<HTMLAnchorElement>) {
  return (
    <a
      href={pathOf(to)}
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return; // let the browser open a new tab
        e.preventDefault();
        navigate(to);
      }}
      {...rest}
    >
      {children}
    </a>
  );
}

export function Logo() {
  return (
    <span className="inline-flex items-center gap-2.5">
      <span className="grid h-[22px] w-[22px] place-items-center rounded-md" style={{ background: "linear-gradient(135deg, #f5a524, #d0581d)" }}>
        <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M2.5 6.5l2.3 2.3L9.5 3.5" fill="none" stroke="#08050f" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>
      </span>
      <span className="text-[16px] font-bold tracking-tight">TradeDesk</span>
    </span>
  );
}

export function SiteHeader({ current }: { current?: Route }) {
  const nav = (to: Route, label: string) => (
    <Link to={to} className="b-link hidden px-1 text-sm sm:inline" aria-current={current === to ? "page" : undefined} style={current === to ? { color: "var(--b-ink)" } : undefined}>
      {label}
    </Link>
  );
  return (
    <header className="border-b" style={{ borderColor: "var(--b-line)" }}>
      <div className="b-wrap flex min-h-[60px] flex-wrap items-center gap-x-5 gap-y-2 py-2">
        <Link to="landing" aria-label="TradeDesk home" className="inline-flex min-h-[44px] items-center" style={{ color: "inherit", textDecoration: "none" }}><Logo /></Link>
        <span className="flex-1" />
        {nav("how", "How it works")}
        {current !== "login" && nav("login", "Log in")}
        <ThemeToggle className="b-muted hover:bg-[var(--b-surface-2)]" />
        <Link to="app" className="b-btn b-btn-primary !min-h-[38px] !px-4 text-sm">Open the desk</Link>
      </div>
    </header>
  );
}

export function SiteFooter() {
  return (
    <footer className="mt-24 border-t" style={{ borderColor: "var(--b-line)" }}>
      <div className="b-wrap flex flex-wrap items-center gap-x-6 gap-y-3 py-8 text-[13px] b-muted">
        <Logo />
        <span>Out of your way. On your side.</span>
        <span className="flex-1" />
        <span>Runs on 021 Trade's sandbox. Facts from your account, never investment advice.</span>
      </div>
    </footer>
  );
}

// A picture of an order card, for the hero and the login page. Not a working card: nothing here can be clicked.
const FP = "b2e5b6109643c398";
export function CardPreview({ compact = false }: { compact?: boolean }) {
  const bars = FP.split("").map((c, i) => {
    const v = parseInt(c, 16);
    return <span key={i} style={{ width: 5, height: `${30 + v * 4.4}%`, borderRadius: 2, background: `hsl(${(v * 22 + i * 9) % 360} 55% 60%)` }} />;
  });
  return (
    <div role="img" aria-label="Example order card: buy 10 Infosys, a draft that has not been sent, waiting for approval" className="b-panel" style={{ background: "var(--b-paper)", boxShadow: "inset 0 0 0 1px var(--b-panel-ring), var(--b-shadow)", padding: compact ? "18px 20px" : "22px 24px" }}>
      <div className="flex items-center gap-2.5 text-[11px] font-bold tracking-[.14em]" style={{ fontFamily: "ui-sans-serif, system-ui, sans-serif" }}>
        <span style={{ color: "var(--b-gain)" }}>BUY</span>
        <span className="b-muted">DRAFT · NOT SENT</span>
        <span className="flex-1" />
        <span className="n tracking-normal" style={{ color: "var(--b-info)" }}>52s</span>
      </div>
      <div className="serif n mt-2.5" style={{ fontSize: compact ? 23 : 28 }}>Buy 10 × INFY</div>
      <div className="b-muted text-[13px]">Infosys Ltd · NSE · Delivery</div>
      <dl className="mt-3 space-y-1 border-y py-2 text-[13.5px]" style={{ borderColor: "var(--b-line)" }}>
        <div className="flex"><dt className="b-muted">Price</dt><dd className="n ml-auto">₹1,450.00 limit</dd></div>
        <div className="flex"><dt className="b-muted">Total, with charges</dt><dd className="n ml-auto">₹14,522.73</dd></div>
      </dl>
      <div className="mt-3 flex items-center gap-3">
        <span className="inline-flex h-[22px] items-end gap-[2px]" aria-hidden="true">{bars}</span>
        <span className="n b-muted text-[11px]">b2e5 b610 9643 c398</span>
      </div>
      <div className="mt-4 grid grid-cols-2 gap-2.5" aria-hidden="true">
        <span className="grid h-11 place-items-center rounded-[10px] text-sm font-bold" style={{ border: "1.5px solid var(--b-ink)" }}>Decline</span>
        <span className="grid h-11 place-items-center rounded-[10px] text-sm font-bold" style={{ background: "var(--b-ink)", color: "var(--b-ink-inverse)" }}>Approve</span>
      </div>
    </div>
  );
}
