export const AUDIO_TYPES = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4", "audio/webm", "audio/ogg", "audio/wav"];

export function recordingType(supports: (mime: string) => boolean): string | undefined {
  return AUDIO_TYPES.find(supports);
}

/** Render these as React text nodes, never HTML. Includes Indian grouping and Hindi digits. */
export function transcriptParts(text: string): { text: string; number: boolean }[] {
  return text.split(/([+−-]?\p{Nd}+(?:[,\.]\p{Nd}+)*%?)/u)
    .filter(Boolean).map((part) => ({ text: part, number: /\p{Nd}/u.test(part) }));
}

export type VoiceState = {
  phase: "idle" | "requesting" | "recording" | "transcribing" | "error";
  seconds: number;
  error?: string;
  denied?: boolean;
};

/** Browser recording lifecycle, separate from React so races can be tested with fakes. */
export class VoiceRecording {
  private recorder?: MediaRecorder;
  private stream?: MediaStream;
  private timer?: ReturnType<typeof setInterval>;
  private deadline?: ReturnType<typeof setTimeout>;
  private abort?: AbortController;
  private generation = 0;
  private active = false;

  constructor(
    private update: (state: VoiceState) => void,
    private transcript: (text: string) => void,
    private upload: (audio: Blob, signal: AbortSignal) => Promise<{ ok: true; data: { text: string } } | { ok: false; message: string }>,
  ) {}

  async start(mime: string) {
    if (this.active) return;
    this.active = true;
    const generation = ++this.generation;
    this.update({ phase: "requesting", seconds: 0 });
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (generation !== this.generation) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      this.stream = stream;
      const recorder = new MediaRecorder(stream, { mimeType: mime });
      this.recorder = recorder;
      const chunks: Blob[] = [];
      let size = 0;
      recorder.ondataavailable = (event) => {
        if (generation !== this.generation || !event.data.size) return;
        size += event.data.size;
        if (size > 5 * 1024 * 1024) {
          this.fail("The recording is too large. Try a shorter recording or type instead.");
          return;
        }
        chunks.push(event.data);
      };
      recorder.onerror = () => this.fail("Couldn't record that. Please try again or type instead.");
      recorder.onstop = async () => {
        if (generation !== this.generation) return;
        this.release();
        const audio = new Blob(chunks, { type: recorder.mimeType || mime });
        chunks.length = 0;
        recorder.ondataavailable = null;
        recorder.onstop = null;
        recorder.onerror = null;
        this.recorder = undefined;
        this.update({ phase: "transcribing", seconds: 0 });
        const abort = new AbortController();
        this.abort = abort;
        try {
          const result = await this.upload(audio, abort.signal);
          if (generation !== this.generation) return;
          if (!result.ok) { this.fail(result.message); return; }
          this.active = false;
          this.transcript(result.data.text);
          this.update({ phase: "idle", seconds: 0 });
        } catch {
          if (generation === this.generation) this.fail("Couldn't transcribe that, please type it");
        }
      };
      recorder.start(1000);
      const started = Date.now();
      this.update({ phase: "recording", seconds: 0 });
      this.timer = setInterval(() => this.update({ phase: "recording", seconds: Math.min(30, Math.floor((Date.now() - started) / 1000)) }), 250);
      this.deadline = setTimeout(() => this.stop(), 30_000);
    } catch (error) {
      if (generation !== this.generation) return;
      const denied = error instanceof DOMException && ["NotAllowedError", "SecurityError"].includes(error.name);
      this.fail(denied ? "Microphone access is blocked. Allow it in your browser settings and reload, or type instead." : "Microphone unavailable. Please type instead.", denied);
    }
  }

  stop() {
    if (this.recorder?.state === "recording") this.recorder.stop();
    // Stop capturing immediately, not after the transcription finishes.
    this.release();
  }

  cancel() {
    ++this.generation;
    this.abort?.abort();
    if (this.recorder) {
      this.recorder.onstop = null;
      this.recorder.ondataavailable = null;
      this.recorder.onerror = null;
      if (this.recorder.state !== "inactive") this.recorder.stop();
    }
    this.release();
    this.active = false;
  }

  private fail(error: string, denied = false) {
    this.cancel();
    this.update({ phase: "error", seconds: 0, error, denied });
  }

  private release() {
    clearInterval(this.timer);
    clearTimeout(this.deadline);
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = undefined;
  }
}
