// Typed calls to the backend. Money only ever moves through the two approve calls, and each one
// must echo the hash of the exact card the trader is looking at.
import type { ApprovalConflict, ChatReply, ExecutionResult, Plan, PlanReport, PendingOrder, Rule } from "./types";

export type Result<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; message: string; conflict?: ApprovalConflict };

async function request<T>(method: string, url: string, body?: unknown): Promise<Result<T>> {
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    return { ok: false, status: 0, message: "Can't reach the server. Check that the backend is running." };
  }
  const text = await res.text();
  let json: unknown = undefined;
  try {
    json = text ? JSON.parse(text) : undefined;
  } catch {
    /* not JSON */
  }
  if (res.ok) return { ok: true, data: json as T };

  const obj = (json ?? {}) as Record<string, unknown>;
  if (res.status === 409 && typeof obj.code === "string") {
    const conflict = obj as unknown as ApprovalConflict;
    return { ok: false, status: 409, message: conflict.message, conflict };
  }
  if (res.status === 503) {
    return { ok: false, status: 503, message: "The broker or assistant isn't reachable right now. Nothing was sent." };
  }
  if (res.status === 422) {
    return { ok: false, status: 422, message: "That request wasn't valid." };
  }
  const detail = typeof obj.detail === "string" ? obj.detail : `Request failed (${res.status}).`;
  return { ok: false, status: res.status, message: detail };
}

export const api = {
  chat: (message: string) => request<ChatReply>("POST", "/api/chat", { message }),

  approveOrder: (id: string, orderHash: string) =>
    request<ExecutionResult>("POST", `/api/approvals/${encodeURIComponent(id)}/approve`, { order_hash: orderHash }),
  declineOrder: (id: string) => request<PendingOrder>("POST", `/api/approvals/${encodeURIComponent(id)}/reject`),

  approvePlan: (id: string, planHash: string) =>
    request<PlanReport>("POST", `/api/plans/${encodeURIComponent(id)}/approve`, { plan_hash: planHash }),
  declinePlan: (id: string) => request<Plan>("POST", `/api/plans/${encodeURIComponent(id)}/reject`),

  cancelRule: (id: string) => request<Rule>("DELETE", `/api/rules/${encodeURIComponent(id)}`),

  auditExportUrl: "/api/audit/export",
};
