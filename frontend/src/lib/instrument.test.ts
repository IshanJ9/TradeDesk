import { describe, expect, it } from "vitest";
import { instrumentLabel, isOption, sizeText } from "./instrument";
import { buysAnOption } from "./limits";
import type { Instrument } from "./types";

const share = { symbol: "INFY", exchange: "NSE", name: "Infosys Ltd" } as Instrument;
const call = {
  symbol: "NIFTY26101324500CE", exchange: "NSE", name: "", series: "OPT", underlying: "NIFTY", lot_size: 75,
  expiry: "2026-10-13", strike: 2450000, option_type: "CE",
} as Instrument;

describe("instrument names", () => {
  it("names an option the way a trader does, and shares by symbol", () => {
    expect(instrumentLabel(call)).toBe("NIFTY 24,500 CE (13 Oct 2026)");
    expect(instrumentLabel(share)).toBe("INFY");
    expect(isOption(call) && !isOption(share)).toBe(true);
  });
  it("counts an option in lots", () => {
    expect(sizeText(75, call)).toBe("1 lot (75 units)");
    expect(sizeText(150, call)).toBe("2 lots (150 units)");
    expect(sizeText(10, share)).toBe("10");
  });
  it("asks for the typed acknowledgment on an option buy, not on selling one held", () => {
    expect(buysAnOption({ side: "BUY", instrument: call })).toBe(true);
    expect(buysAnOption({ side: "SELL", instrument: call })).toBe(false);
    expect(buysAnOption({ side: "BUY", instrument: share })).toBe(false);
  });
});
