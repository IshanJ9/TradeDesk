import { useCallback, useReducer, useState } from "react";
import { AccountPanel } from "./components/AccountPanel";
import { ApprovalDock } from "./components/ApprovalDock";
import { ChatPanel } from "./components/ChatPanel";
import { DemoControls } from "./components/DemoControls";
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
          <svg width="22" height="22" viewBox="0 0 32 32" aria-hidden="true">
            <rect width="32" height="32" rx="7" fill="var(--ink)" />
            <path d="M9 11h14M9 16h14M9 21h8" stroke="var(--ink-inverse)" strokeWidth="2.4" strokeLinecap="round" />
          </svg>
          <span className="text-[15px] font-semibold tracking-tight">TradeDesk</span>
          <span className="hidden text-xs text-muted sm:inline">Out of your way. On your side.</span>
        </div>
        <div className="flex items-center gap-3 text-xs">
          <DemoControls notify={(message) => dispatch({ type: "toast", toast: { kind: "info", message } })} />
          {locks?.anchor_active && <span className="rounded-full border border-[var(--info-line)] bg-[var(--info-bg)] px-2 py-0.5 text-[var(--info-ink)]">Anchor on</span>}
          {locks?.buffett_mode && <span className="rounded-full border border-line px-2 py-0.5 text-muted">Buffett Mode</span>}
          {locks?.co_captain_locked && <span className="rounded-full border border-[var(--info-line)] bg-[var(--info-bg)] px-2 py-0.5 text-[var(--info-ink)]">Co-Captain lock</span>}
          <span role="status" className="inline-flex items-center gap-1.5 text-muted">
            <span className={`h-2 w-2 rounded-full ${status.dot}`} aria-hidden="true" />
            {status.text}
          </span>
        </div>
      </header>

      <main className="grid min-h-0 flex-1 lg:grid-cols-[minmax(340px,5fr)_minmax(460px,6fr)]">
        <div className={`min-h-0 border-line bg-surface lg:border-r ${view === "ask" ? "block" : "max-lg:hidden"}`}>
          <ChatPanel messages={messages} busy={busy} offline={state.conn !== "live"} send={send} onReveal={reveal} />
        </div>
        <div className={`min-h-0 overflow-y-auto scroll-quiet ${view === "desk" ? "block" : "max-lg:hidden"}`}>
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
