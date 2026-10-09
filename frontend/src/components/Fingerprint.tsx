import type { CSSProperties } from "react";
import { fingerprint } from "../lib/fingerprint";
import { shortHash } from "../lib/format";

/** The exact order (or plan) the Approve button refers to, drawn from its hash. */
export function Fingerprint({ hash, label = "Approval fingerprint" }: { hash: string; label?: string }) {
  return (
    <div className="flex items-center gap-3" title={hash}>
      {/* key={hash}: if the order changes, the strip redraws so the change is visible */}
      <span className="fp" key={hash} aria-hidden="true">
        {fingerprint(hash).map((c, i) => (
          <span key={i} style={{ "--h": c.hue, "--k": c.height, "--i": i } as CSSProperties} />
        ))}
      </span>
      <div className="leading-tight">
        <div className="text-[11px] uppercase tracking-[0.08em] text-muted">{label}</div>
        <div className="num text-[12px] text-ink">{shortHash(hash)}</div>
      </div>
    </div>
  );
}
