import { useCallback, useEffect, useReducer, useState } from "react";
import { AccountMenu } from "./components/AccountMenu";
import { AccountPanel } from "./components/AccountPanel";
import { ApprovalDock } from "./components/ApprovalDock";
import { ChatPanel } from "./components/ChatPanel";
import { DemoControls } from "./components/DemoControls";
import { ThemeToggle } from "./components/ThemeToggle";
import { forgetBroker, reconnectBroker, refreshBroker, SIMULATED, useBroker } from "./lib/broker";
import { navigate } from "./lib/router";
import { DeskTabs } from "./components/DeskTabs";
import { Toasts } from "./components/Toasts";
import { awaiting, initialState, reducer } from "./lib/store";
import { useChat } from "./lib/useChat";
import { useLiveFeed } from "./lib/ws";

type View = "ask" | "desk";

const STATUS = {
  live: { dot: "bg-gain", text: "Live" },
  connecting: { dot: "bg-[var(--warn-line)]", text: "Connecting…" },
  offline: { dot: "bg-loss", text: "Offline, reconnecting…" },
} as const;

export default function App() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const { messages, busy, send } = useChat();
  const [view, setView] = useState<View>("ask");
  useLiveFeed(dispatch);
  const broker = useBroker();
  const [reconnecting, setReconnecting] = useState<string | null>(null); // null = idle, "" = working, text = last failure
  useEffect(() => {
    void refreshBroker();
    const t = setInterval(() => void refreshBroker(), 20_000); // notices a session 021 revoked, or a changed account
    return () => { clearInterval(t); forgetBroker(); };
  }, []);
  useEffect(() => { if (state.conn === "live") void refreshBroker(); }, [state.conn]);
  const accountKind = state.account?.account_kind ?? broker?.kind;
  async function reconnect() {
    setReconnecting("");
    const r = await reconnectBroker();
    setReconnecting(r.ok ? null : r.message);
  }

  const waiting = awaiting(state);
  const waitingCount = waiting.orders.length + waiting.plans.length;
  const status = STATUS[state.conn];
  const locks = state.account?.locks;

  /** Bring a ticket into view (and, on a phone, switch to the desk first). */
  const reveal = useCallback((id: string) => {
    setView("desk");
    setTimeout(() => {
      const el = document.getElementById(`card-${id}`);
      el?.scrollIntoView({ behavior: "smooth", block: "center" });
      el?.classList.add("flash");
      setTimeout(() => el?.classList.remove("flash"), 1400);
    }, 60);
  }, []);

  const ask = useCallback((text: string) => { setView("ask"); void send(text); }, [send]);

  return (
    <div className="flex h-dvh flex-col">
      <header className="flex h-[52px] shrink-0 items-center justify-between gap-3 border-b border-line bg-surface px-4">
        <div className="flex items-center gap-2.5">
          <a href="/" onClick={(e) => { if (e.metaKey || e.ctrlKey) return; e.preventDefault(); navigate("landing"); }} aria-label="TradeDesk home" className="flex items-center gap-2.5 text-ink no-underline">
            <svg width="22" height="22" viewBox="0 0 32 32" aria-hidden="true">
              <rect width="32" height="32" rx="7" fill="var(--ink)" />
              <path d="M9 11h14M9 16h14M9 21h8" stroke="var(--ink-inverse)" strokeWidth="2.4" strokeLinecap="round" />
            </svg>
            <span className="text-[15px] font-semibold tracking-tight">TradeDesk</span>
          </a>
          <span className="hidden text-xs text-muted sm:inline">Out of your way. On your side.</span>
        </div>
        <div className="flex items-center gap-3 text-xs">
          {accountKind === "mock" && (
            <span className="inline-flex items-center rounded-full border border-[var(--info-line)] bg-[var(--info-bg)] px-2 py-0.5 text-[var(--info-ink)]" title={SIMULATED} aria-label={SIMULATED}>
              <span className="sm:hidden">Simulated</span><span className="hidden sm:inline">{SIMULATED}</span>
            </span>
          )}
          <DemoControls notify={(message) => dispatch({ type: "toast", toast: { kind: "info", message } })} />
          <ThemeToggle className="text-muted hover:bg-surface2" />
          <AccountMenu />
          {locks?.anchor_active && <span className="rounded-full border border-[var(--info-line)] bg-[var(--info-bg)] px-2 py-0.5 text-[var(--info-ink)]">Anchor on</span>}
          {locks?.buffett_mode && <span className="rounded-full border border-line px-2 py-0.5 text-muted">Buffett Mode</span>}
          {locks?.co_captain_locked && <span className="rounded-full border border-[var(--info-line)] bg-[var(--info-bg)] px-2 py-0.5 text-[var(--info-ink)]">Co-Captain lock</span>}
          <span role="status" className="inline-flex items-center gap-1.5 text-muted">
            <span className={`h-2 w-2 rounded-full ${status.dot}`} aria-hidden="true" />
            {status.text}
          </span>
        </div>
      </header>

      {broker?.status === "needs_reconnect" && (
        <div role="alert" className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-[var(--warn-line)] bg-[var(--warn-bg)] px-4 py-2 text-[13px] text-[var(--warn-ink)]">
          <span className="min-w-0 flex-1">Your 021 account isn't connected, so nothing can be sent. {reconnecting ? reconnecting : "Reconnect to carry on."}</span>
          <button type="button" onClick={() => void reconnect()} disabled={reconnecting === ""} className="min-h-[36px] rounded-lg border border-[var(--warn-line)] px-3 font-medium disabled:opacity-60">
            {reconnecting === "" ? "Reconnecting…" : "Reconnect"}
          </button>
        </div>
      )}

      <main className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(340px,5fr)_minmax(460px,6fr)]">
        <div className={`min-h-0 min-w-0 border-line bg-surface lg:border-r ${view === "ask" ? "block" : "max-lg:hidden"}`}>
          <ChatPanel messages={messages} busy={busy} offline={state.conn !== "live"} send={send} onReveal={reveal} trace={state.trace} />
        </div>
        <div className={`min-h-0 min-w-0 overflow-y-auto scroll-quiet ${view === "desk" ? "block" : "max-lg:hidden"}`}>
          <ApprovalDock state={state} dispatch={dispatch} />
          <div className="pb-2" />
          <AccountPanel account={state.account} live={state.conn === "live"} />
          <DeskTabs state={state} dispatch={dispatch} ask={ask} />
        </div>
      </main>

      <nav className="grid shrink-0 grid-cols-2 border-t border-line bg-surface lg:hidden" aria-label="Switch view">
        {(["ask", "desk"] as const).map((v) => (
          <button
            key={v}
            onClick={() => setView(v)}
            aria-current={view === v}
            className={`py-3 text-[13px] font-medium ${view === v ? "text-ink" : "text-muted"}`}
          >
            {v === "ask" ? "Ask" : "Desk"}
            {v === "desk" && waitingCount > 0 && (
              <span className="ml-1.5 rounded-full bg-ink px-1.5 py-px text-[11px] text-inverse">{waitingCount}</span>
            )}
          </button>
        ))}
      </nav>

      <Toasts toasts={state.toasts} dispatch={dispatch} onReveal={reveal} />
    </div>
  );
}
