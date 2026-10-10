// Warnings written by the trader's own limits (app/risk) start with "You set" or "You switched". A card carrying
// one needs a deliberate "I've read this" tick before Approve: friction, not a block (the backend blocks only
// limits the trader switched to hard).
export function crossesOwnLimit(warnings: readonly string[]): boolean {
  return warnings.some((w) => w.startsWith("You set") || w.startsWith("You switched"));
}

// Buying an option can lose the whole premium: that card always asks for the typed acknowledgment too.
export function buysAnOption(o: { side?: string | null; instrument: { option_type?: string | null } }): boolean {
  return o.side === "BUY" && o.instrument.option_type != null;
}

// The server marks cards whose loss can exceed what the trader puts in (futures, an opened short). Approve is
// refused there without the typed words, whatever this screen does; the screen just asks first.
export function needsTypedAck(o: { risk_ack_required?: boolean | null }): boolean {
  return o.risk_ack_required === true;
}

export function riskAcknowledged(value: string): boolean {
  return value.trim() === "I UNDERSTAND";
}
