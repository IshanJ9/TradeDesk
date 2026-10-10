// What a Co-Captain sees: their review inbox and nothing of the trader's account.
import { useReducer } from "react";
import { useCoCaptain } from "../lib/cocaptain";
import { navigate } from "../lib/router";
import { initialState, reducer } from "../lib/store";
import { CoCaptainPanel } from "./CoCaptainPanel";
import { ThemeToggle } from "./ThemeToggle";
import { Toasts } from "./Toasts";

export function ReviewerHome() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const view = useCoCaptain();
  return (
    <div className="mx-auto flex min-h-dvh max-w-[40rem] flex-col px-4 pb-10">
      <header className="flex h-[52px] items-center justify-between">
        <a href="/" onClick={(e) => { if (e.metaKey || e.ctrlKey) return; e.preventDefault(); navigate("landing"); }} className="text-[15px] font-semibold tracking-tight text-ink no-underline">TradeDesk</a>
        <ThemeToggle />
      </header>
      <h1 className="mb-2 font-serif text-2xl text-ink">Co-Captain</h1>
      <CoCaptainPanel view={view} dispatch={dispatch} />
      <Toasts toasts={state.toasts} dispatch={dispatch} onReveal={() => {}} />
    </div>
  );
}
