// The approval is bound to a hash of the exact order. This turns that hash into a small visual
// fingerprint, so a card that changes (for example after a price move) visibly looks different
// and the trader can see that "Approve" refers to this precise order.

export interface Cell {
  hue: number; // 0-360
  height: number; // 0-1
}

/** 16 cells, derived from the first 32 hex characters of the hash. Same hash -> same picture. */
export function fingerprint(hash: string): Cell[] {
  const clean = hash.toLowerCase().replace(/[^0-9a-f]/g, "").padEnd(32, "0");
  const cells: Cell[] = [];
  for (let i = 0; i < 16; i++) {
    const a = parseInt(clean[i]!, 16);
    const b = parseInt(clean[16 + i]!, 16);
    cells.push({ hue: Math.round((a / 16) * 360), height: 0.35 + (b / 15) * 0.65 });
  }
  return cells;
}
