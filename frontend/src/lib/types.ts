// Friendly names for the generated contract (types.gen.ts). Never hand-write a shape here:
// every type comes from the backend's Pydantic models.
import type { components, paths } from "./types.gen";

type S = components["schemas"];

export type Account = S["AccountSnapshot"];
export type Holding = S["Holding"];
export type Position = S["Position"];
export type Locks = S["AccountLocks"];
export type Order = S["Order"];
export type PendingOrder = S["PendingOrder"];
export type Plan = S["Plan"];
export type PlanLeg = S["PlanLeg"];
export type PlanReport = S["PlanReport"];
export type PlanLegResult = S["PlanLegResult"];
export type Rule = S["Rule"];
export type AuditEvent = S["AuditEvent"];
export type Charges = S["Charges"];
export type Instrument = S["Instrument"];
export type ExecutionResult = S["ExecutionResult"];
export type ApprovalConflict = S["ApprovalConflict"];
export type ChatReply = S["ChatReply"];
export type Card = ChatReply["cards"][number];
export type Tick = S["Tick"];

/** Every message that can arrive on /ws. */
export type WsEvent =
  paths["/api/ws-events"]["get"]["responses"]["200"]["content"]["application/json"][number];
