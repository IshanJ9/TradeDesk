import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { transcribe, voiceError } from "./api";
import { recordingType, transcriptParts, voiceWhere, VoiceRecording, type VoiceState, type VoiceStatus } from "./voice";

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); vi.restoreAllMocks(); });

describe("editable voice text", () => {
  it("highlights Indian grouping, decimals, signs, percentages and Hindi digits without changing text", () => {
    const text = "Buy १० INFY at 1,450.50; loss −3%, budget 1,00,000";
    const parts = transcriptParts(text);
    expect(parts.map((p) => p.text).join("")).toBe(text);
    expect(parts.filter((p) => p.number).map((p) => p.text)).toEqual(["१०", "1,450.50", "−3%", "1,00,000"]);
  });
  it("keeps outside text literal, including markup", () => {
    const text = '<img src=x onerror="alert(1)"> buy 5';
    expect(transcriptParts(text).map((p) => p.text).join("")).toBe(text);
    expect(transcriptParts("")).toEqual([]);
  });
  it("selects only an accepted and supported recording format", () => {
    expect(recordingType((type) => type === "audio/mp4")).toBe("audio/mp4");
    expect(recordingType(() => false)).toBeUndefined();
    expect(recordingType(() => true)).toBe("audio/webm;codecs=opus");
  });
});

describe("where the audio goes", () => {
  const status = (s: Partial<VoiceStatus>): VoiceStatus => ({ provider: "groq", fallback: false, local_ready: false, groq_ready: true, ...s });
  it("says so plainly for every setting", () => {
    expect(voiceWhere(null)).toBe("Voice uses Groq transcription.");
    expect(voiceWhere(status({}))).toBe("Voice uses Groq transcription.");
    expect(voiceWhere(status({ groq_ready: false }))).toBe("Voice isn't set up on this server.");
    expect(voiceWhere(status({ provider: "local", local_ready: true }))).toBe("Voice runs on this laptop; audio never leaves it.");
    expect(voiceWhere(status({ provider: "local", local_ready: true, fallback: true }))).toBe("Voice runs on this laptop (Groq only if it fails).");
    expect(voiceWhere(status({ provider: "local", fallback: true }))).toBe("Local voice isn't set up, so Groq transcribes.");
    expect(voiceWhere(status({ provider: "local", groq_ready: false }))).toBe("Local voice isn't set up on this server.");
  });
});

describe("transcription API", () => {
  it("posts raw audio only to transcription, never chat or approval", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ text: " buy 10 INFY ", seconds: 4 })));
    vi.stubGlobal("fetch", fetch);
    const blob = new Blob(["audio"], { type: "audio/webm;codecs=opus" });
    expect(await transcribe(blob)).toEqual({ ok: true, data: { text: "buy 10 INFY", seconds: 4, provider: "groq", fell_back: false } });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0]![0]).toBe("/api/voice/transcribe");
    expect(fetch.mock.calls[0]![1]).toMatchObject({ method: "POST", body: blob, headers: { "Content-Type": blob.type } });
  });
  it("passes on which provider transcribed it, and whether local voice fell back to Groq", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ text: "x", seconds: 1, provider: "groq", fell_back: true }))));
    expect(await transcribe(new Blob())).toMatchObject({ ok: true, data: { provider: "groq", fell_back: true } });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ text: "x", provider: "local", fell_back: "yes" }))));
    expect(await transcribe(new Blob())).toMatchObject({ ok: true, data: { provider: "local", fell_back: false } });
  });
  it.each([400, 413, 415, 429, 502, 503])( "maps %i to safe voice-specific wording", async (status) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("secret raw error", { status })));
    expect(await transcribe(new Blob())).toEqual({ ok: false, status, message: voiceError(status) });
    expect(voiceError(status)).not.toContain("secret");
    expect(voiceError(503)).toBe("Voice is not set up on this server");
  });
  it.each([{}, { text: "" }, { text: 7 }, { text: "x".repeat(501) }])("rejects malformed success %j", async (data) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(data))));
    expect((await transcribe(new Blob())).ok).toBe(false);
  });
  it("maps network failures without echoing errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("secret")));
    expect(await transcribe(new Blob())).toEqual({ ok: false, status: 0, message: voiceError(0) });
  });
  it("aborts a request after 25 seconds", async () => {
    vi.useFakeTimers();
    let signal: AbortSignal;
    vi.stubGlobal("fetch", vi.fn((_url, options) => new Promise((_resolve, reject) => {
      signal = options.signal;
      signal.addEventListener("abort", () => reject(new Error("aborted")));
    })));
    const result = transcribe(new Blob());
    await vi.advanceTimersByTimeAsync(25_000);
    expect(signal!.aborted).toBe(true);
    expect((await result).ok).toBe(false);
  });
});

class FakeRecorder {
  static instance: FakeRecorder;
  state = "inactive";
  mimeType = "audio/webm";
  ondataavailable: ((e: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() { FakeRecorder.instance = this; }
  start() { this.state = "recording"; }
  stop() {
    this.state = "inactive";
    this.ondataavailable?.({ data: new Blob(["sound"]) });
    void Promise.resolve().then(() => this.onstop?.());
  }
}

describe("recording lifecycle", () => {
  type Upload = ConstructorParameters<typeof VoiceRecording>[2];
  let stop = vi.fn();
  let getUserMedia = vi.fn<() => Promise<MediaStream>>();
  let stream: MediaStream;
  let states: VoiceState[];
  let transcript = vi.fn<(text: string, fellBack: boolean) => void>();
  let upload = vi.fn<Upload>();
  let recorder: VoiceRecording;
  beforeEach(() => {
    vi.useFakeTimers();
    stop = vi.fn();
    stream = { getTracks: () => [{ stop }] } as unknown as MediaStream;
    getUserMedia = vi.fn().mockResolvedValue(stream);
    vi.stubGlobal("navigator", { mediaDevices: { getUserMedia } });
    vi.stubGlobal("MediaRecorder", FakeRecorder);
    states = [];
    transcript = vi.fn();
    upload = vi.fn().mockResolvedValue({ ok: true, data: { text: "buy 10" } });
    recorder = new VoiceRecording((state) => states.push(state), transcript, upload);
  });
  afterEach(() => recorder.cancel());
  it("records, stops, releases the mic and returns text only after transcription", async () => {
    await recorder.start("audio/webm");
    expect(upload).not.toHaveBeenCalled();
    expect(states.at(-1)?.phase).toBe("recording");
    recorder.stop();
    await vi.advanceTimersByTimeAsync(0);
    expect(stop).toHaveBeenCalled();
    expect(upload).toHaveBeenCalledTimes(1);
    expect(transcript).toHaveBeenCalledWith("buy 10", false);
    expect(states.map((s) => s.phase)).toEqual(["requesting", "recording", "transcribing", "idle"]);
  });
  it("tells the chat when local voice fell back to Groq", async () => {
    upload.mockResolvedValue({ ok: true, data: { text: "buy 10", fell_back: true } });
    await recorder.start("audio/webm");
    recorder.stop();
    await vi.advanceTimersByTimeAsync(0);
    expect(transcript).toHaveBeenCalledWith("buy 10", true);
  });
  it("auto-stops at exactly 30 seconds", async () => {
    await recorder.start("audio/webm");
    await vi.advanceTimersByTimeAsync(29_999);
    expect(upload).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(upload).toHaveBeenCalledTimes(1);
    expect(stop).toHaveBeenCalled();
  });
  it("does not open multiple microphones on a double click", async () => {
    await Promise.all([recorder.start("audio/webm"), recorder.start("audio/webm")]);
    expect(getUserMedia).toHaveBeenCalledTimes(1);
  });
  it("cleans up permission arriving after unmount", async () => {
    let resolve!: (stream: MediaStream) => void;
    getUserMedia.mockReturnValue(new Promise((r) => { resolve = r; }));
    const start = recorder.start("audio/webm");
    recorder.cancel();
    resolve(stream);
    await start;
    expect(stop).toHaveBeenCalled();
    expect(upload).not.toHaveBeenCalled();
  });
  it("aborts uploads and ignores late transcripts after unmount", async () => {
    let resolve!: (value: Awaited<ReturnType<Upload>>) => void;
    upload.mockReturnValue(new Promise((r) => { resolve = r; }));
    await recorder.start("audio/webm");
    recorder.stop();
    await vi.advanceTimersByTimeAsync(0);
    recorder.cancel();
    resolve({ ok: true, data: { text: "old transcript" } });
    await vi.advanceTimersByTimeAsync(0);
    expect(upload.mock.calls[0]![1].aborted).toBe(true);
    expect(transcript).not.toHaveBeenCalled();
  });
  it("permission denial is actionable and does not upload", async () => {
    getUserMedia.mockRejectedValue(new DOMException("private details", "NotAllowedError"));
    await recorder.start("audio/webm");
    expect(states.at(-1)).toMatchObject({ phase: "error", denied: true });
    expect(states.at(-1)?.error).toContain("type instead");
    expect(states.at(-1)?.error).not.toContain("private details");
    expect(upload).not.toHaveBeenCalled();
  });
  it("recording failure releases the mic and never uploads", async () => {
    await recorder.start("audio/webm");
    FakeRecorder.instance.onerror?.();
    await vi.advanceTimersByTimeAsync(0);
    expect(stop).toHaveBeenCalled();
    expect(states.at(-1)?.phase).toBe("error");
    expect(upload).not.toHaveBeenCalled();
  });
  it("limits buffered audio to 5 MB", async () => {
    await recorder.start("audio/webm");
    FakeRecorder.instance.ondataavailable?.({ data: new Blob([new Uint8Array(5 * 1024 * 1024 + 1)]) });
    await vi.advanceTimersByTimeAsync(0);
    expect(states.at(-1)?.error).toContain("too large");
    expect(stop).toHaveBeenCalled();
    expect(upload).not.toHaveBeenCalled();
  });
  it("shows a transcription failure without inserting text", async () => {
    upload.mockResolvedValue({ ok: false, message: voiceError(503) });
    await recorder.start("audio/webm");
    recorder.stop();
    await vi.advanceTimersByTimeAsync(0);
    expect(states.at(-1)?.error).toBe(voiceError(503));
    expect(transcript).not.toHaveBeenCalled();
  });
});
