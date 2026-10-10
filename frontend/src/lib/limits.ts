// Warnings written by the trader's own limits (app/risk) start with "You set" or "You switched". A card carrying
// one needs a deliberate "I've read this" tick before Approve: friction, not a block (the backend blocks only
// limits the trader switched to hard).
export function crossesOwnLimit(warnings: readonly string[]): boolean {
  return warnings.some((w) => w.startsWith("You set") || w.startsWith("You switched"));
}

export function riskAcknowledged(value: string): boolean {
  return value.trim() === "I UNDERSTAND";
}
