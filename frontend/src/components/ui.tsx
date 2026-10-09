import { useEffect, useState, type ButtonHTMLAttributes, type ReactNode } from "react";

/** Re-renders its caller every `ms`. Use it only in small components (a countdown), never at the root. */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "quiet" | "plain" };

const buttonStyles = {
  // Approve and Decline are deliberately the same size and weight of border: no nudging.
  primary:
    "bg-ink text-inverse border border-ink hover:opacity-90 disabled:opacity-40 disabled:hover:opacity-40",
  quiet: "bg-transparent text-ink border border-strong hover:bg-surface2 disabled:opacity-40",
  plain: "bg-transparent text-muted hover:text-ink hover:bg-surface2 disabled:opacity-40 border border-transparent",
};

export function Button({ variant = "quiet", className = "", ...rest }: ButtonProps) {
  return (
    <button
      {...rest}
      className={`inline-flex items-center justify-center gap-1.5 rounded-lg px-3.5 py-2 text-[13px] font-medium transition-colors ${buttonStyles[variant]} ${className}`}
    />
  );
}

const bannerStyles = {
  warn: "bg-[var(--warn-bg)] text-[var(--warn-ink)] border-[var(--warn-line)]",
  error: "bg-[var(--error-bg)] text-[var(--error-ink)] border-[var(--error-line)]",
  info: "bg-[var(--info-bg)] text-[var(--info-ink)] border-[var(--info-line)]",
};

export function Banner({ tone, children, role }: { tone: keyof typeof bannerStyles; children: ReactNode; role?: string }) {
  return (
    <div role={role} className={`rounded-lg border px-3 py-2 text-[13px] leading-snug ${bannerStyles[tone]}`}>
      {children}
    </div>
  );
}

export function Section({
  title,
  aside,
  children,
  id,
}: {
  title: string;
  aside?: ReactNode;
  children: ReactNode;
  id?: string;
}) {
  return (
    <section id={id} aria-label={title} className="px-4 pt-4">
      <div className="mb-2.5 flex items-baseline justify-between gap-3">
        <h2 className="font-serif text-[19px] italic leading-none text-ink">{title}</h2>
        {aside && <div className="text-xs text-muted">{aside}</div>}
      </div>
      {children}
    </section>
  );
}

export function Chip({ children, tone = "plain" }: { children: ReactNode; tone?: "plain" | "gain" | "loss" | "warn" | "info" }) {
  const tones = {
    plain: "border-line text-muted",
    gain: "border-[color-mix(in_srgb,var(--gain)_45%,transparent)] text-gain",
    loss: "border-[color-mix(in_srgb,var(--loss)_45%,transparent)] text-loss",
    warn: "border-[var(--warn-line)] text-[var(--warn-ink)] bg-[var(--warn-bg)]",
    info: "border-[var(--info-line)] text-[var(--info-ink)] bg-[var(--info-bg)]",
  };
  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-px text-[11px] font-medium leading-[18px] ${tones[tone]}`}>
      {children}
    </span>
  );
}

/** Signed money in the gain/loss colours. Pass a ready string; the server computed the number. */
export function Signed({ value, children }: { value: number; children: ReactNode }) {
  const tone = value > 0 ? "text-gain" : value < 0 ? "text-loss" : "text-muted";
  return <span className={`num ${tone}`}>{children}</span>;
}

export function Empty({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-strong px-4 py-5 text-center">
      <p className="font-medium text-ink">{title}</p>
      <p className="mx-auto mt-1 max-w-[46ch] text-[13px] text-muted">{children}</p>
    </div>
  );
}
