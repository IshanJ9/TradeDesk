// Typed calls to the backend. Money only ever moves through the two approve calls, and each one
// must echo the hash of the exact card the trader is looking at.
import type { components } from "./types.gen";
import type { ApprovalConflict, ChatReply, ExecutionResult, Plan, PlanReport, PendingOrder, Rule } from "./types";

export type Result<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; message: string; conflict?: ApprovalConflict };

// Who is calling. Only a demo build with COCAPTAIN_DEV_ACTORS honours it (open /app?as=b in a second tab to play the
// Co-Captain); a real deployment ignores the header, so this can never name anyone.
const actor = typeof location === "undefined" ? null : new URLSearchParams(location.search).get("as");

async function request<T>(method: string, url: string, body?: unknown): Promise<Result<T>> {
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers: {
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...(actor ? { "x-tradedesk-actor": actor } : {}),
      },
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

export type CoCaptainSettings = components["schemas"]["PairingStatus"];
export type CoCaptainLink = components["schemas"]["Link"];

export const cocaptainApi = {
  config: () => request<{ enabled: boolean; dev_actors: boolean }>("GET", "/api/cocaptain/config"),
  settings: () => request<CoCaptainSettings>("GET", "/api/cocaptain/settings"),
  invite: (email: string) => request<CoCaptainLink>("POST", "/api/cocaptain/invite", { email }),
  accept: (link: CoCaptainLink) => request<CoCaptainLink>("POST", "/api/cocaptain/accept", { owner_id: link.owner_id, link_id: link.id }),
  revoke: (link: CoCaptainLink) => request<CoCaptainLink>("POST", "/api/cocaptain/revoke", { owner_id: link.owner_id, link_id: link.id }),
  inbox: () => request<PendingOrder[]>("GET", "/api/cocaptain/inbox"),
  approve: (id: string, orderHash: string) =>
    request<ExecutionResult>("POST", `/api/cocaptain/cards/${encodeURIComponent(id)}/approve`, { order_hash: orderHash }),
  decline: (id: string) => request<PendingOrder>("POST", `/api/cocaptain/cards/${encodeURIComponent(id)}/decline`, {}),
};

export const api = {
  chat: (message: string, viaVoice = false) => request<ChatReply>("POST", "/api/chat", { message, via_voice: viaVoice }),

  approveOrder: (id: string, orderHash: string, acknowledgment?: string) =>
    request<ExecutionResult>("POST", `/api/approvals/${encodeURIComponent(id)}/approve`, {
      order_hash: orderHash,
      ...(acknowledgment ? { acknowledgment } : {}),
    }),
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
  audio: Blob, signal?: AbortSignal, language?: "en" | "hi",
): Promise<Result<import("./types.gen").components["schemas"]["Transcript"]>> {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  signal?.addEventListener("abort", cancel, { once: true });
  if (signal?.aborted) cancel();
  const timer = setTimeout(cancel, 25_000);
  try {
    const response = await fetch(`/api/voice/transcribe${language ? `?language=${language}` : ""}`, {
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
    const provider = "provider" in data && data.provider === "local" ? "local" : "groq";
    const fell_back = "fell_back" in data && data.fell_back === true;
    return { ok: true, data: { text: data.text.trim(), seconds, provider, fell_back } };
  } catch {
    return { ok: false, status: 0, message: voiceError(0) };
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", cancel);
  }
}

/** Where recordings are transcribed (Groq or this machine). Read-only. */
export const voiceStatus = () => request<import("./types.gen").components["schemas"]["VoiceStatus"]>("GET", "/api/voice/status");

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
