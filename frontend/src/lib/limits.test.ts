import { describe, expect, it } from "vitest";
import { crossesOwnLimit, riskAcknowledged } from "./limits";

describe("crossesOwnLimit", () => {
  it("is true for the trader's own limit warnings", () => {
    expect(crossesOwnLimit(["You set a 10% single-order limit; this order is approximately 13.38% of your portfolio."])).toBe(true);
    expect(crossesOwnLimit(["You switched on a hard limit of 2 orders a day."])).toBe(true);
  });

  it("is false for other warnings and for none", () => {
    expect(crossesOwnLimit([])).toBe(false);
    expect(crossesOwnLimit(["Stop-loss: nothing happens until ₹1,400.00 is reached."])).toBe(false);
  });
});

describe("risk warning confirmation", () => {
  it("requires the full explicit phrase", () => {
    expect(riskAcknowledged("I UNDERSTAND")).toBe(true);
    expect(riskAcknowledged(" I UNDERSTAND ")).toBe(true);
    for (const text of ["", "yes", "I", "i understand", "I UNDERSTAND nothing"]) {
      expect(riskAcknowledged(text)).toBe(false);
    }
  });
});
