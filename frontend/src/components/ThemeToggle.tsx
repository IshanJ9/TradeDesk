// System / Light / Dark. Used on the public pages and on the desk.
import { useState } from "react";
import { applyTheme, nextTheme, readTheme, type ThemeChoice } from "../lib/theme";

const LABEL: Record<ThemeChoice, string> = { system: "System", light: "Light", dark: "Dark" };

function Icon({ choice }: { choice: ThemeChoice }) {
  const common = { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round" as const, strokeLinejoin: "round" as const, "aria-hidden": true };
  if (choice === "light")
    return <svg {...common}><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>;
  if (choice === "dark") return <svg {...common}><path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z" /></svg>;
  return <svg {...common}><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M8 20h8M12 16v4" /></svg>;
}

export function ThemeToggle({ className = "" }: { className?: string }) {
  const [choice, setChoice] = useState<ThemeChoice>(readTheme);
  const next = nextTheme(choice);
  return (
    <button
      type="button"
      onClick={() => { applyTheme(next); setChoice(next); }}
      aria-label={`Theme: ${LABEL[choice]}. Switch to ${LABEL[next]}`}
      title={`Theme: ${LABEL[choice]}`}
      className={`inline-flex min-h-[36px] items-center gap-1.5 rounded-lg px-2.5 text-xs ${className}`}
    >
      <Icon choice={choice} />
      <span className="hidden sm:inline">{LABEL[choice]}</span>
    </button>
  );
}
