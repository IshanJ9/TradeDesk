import { describe, expect, it } from "vitest";
import { crossesOwnLimit } from "./limits";

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
