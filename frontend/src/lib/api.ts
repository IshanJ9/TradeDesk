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

// voice-live: raw audio only; the returned text is never submitted to chat here.
export function voiceError(status: number): string {
  switch (status) {
    case 400: return "The recording is empty. Please try again or type instead.";
    case 413: return "The recording is too large. Try a shorter recording or type instead.";
    case 415: return "This recording format isn't supported. Please type instead.";
    case 429: return "Too many voice requests, wait a moment or type instead";
    case 503: return "Voice is not set up on this server";
    default: return "Couldn't transcribe that, please type it";
  }
}

export async function transcribe(
  audio: Blob, signal?: AbortSignal,
): Promise<Result<import("./types.gen").components["schemas"]["Transcript"]>> {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  signal?.addEventListener("abort", cancel, { once: true });
  if (signal?.aborted) cancel();
  const timer = setTimeout(cancel, 25_000);
  try {
    const response = await fetch("/api/voice/transcribe", {
      method: "POST", body: audio, signal: controller.signal,
      headers: { "Content-Type": audio.type || "application/octet-stream" },
    });
    if (!response.ok) return { ok: false, status: response.status, message: voiceError(response.status) };
    const data: unknown = await response.json();
    if (typeof data !== "object" || data === null || !("text" in data) ||
        typeof data.text !== "string" || !data.text.trim() || data.text.length > 500) {
      return { ok: false, status: 502, message: voiceError(502) };
    }
    const seconds = "seconds" in data && typeof data.seconds === "number" &&
      Number.isFinite(data.seconds) && data.seconds >= 0 ? data.seconds : null;
    return { ok: true, data: { text: data.text.trim(), seconds } };
  } catch {
    return { ok: false, status: 0, message: voiceError(0) };
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", cancel);
  }
}

// risk-goals: independent settings/report calls; no order actions.
type RiskSchemas = import("./types.gen").components["schemas"];
export const disciplineApi = {
  report: () => request<RiskSchemas["DisciplineReport"]>("GET", "/api/discipline"),
  presets: () => request<RiskSchemas["PresetOption"][]>("GET", "/api/profile/presets"),
  suggest: (body: RiskSchemas["OnboardingAnswers"]) => request<RiskSchemas["OnboardingSuggestion"]>("POST", "/api/profile/onboarding", body),
  saveProfile: (body: RiskSchemas["RiskProfile-Input"]) => request<RiskSchemas["RiskProfile-Output"]>("PUT", "/api/profile", body),
  saveGoal: (body: RiskSchemas["GoalRequest"]) => request<RiskSchemas["Goal"]>("PUT", "/api/goal", body),
  deleteGoal: () => request<void>("DELETE", "/api/goal"),
};
