import { useEffect, useRef, useState } from "react";
import { transcribe } from "../lib/api";
import { recordingType, VoiceRecording, type VoiceState } from "../lib/voice";
import { Button } from "./ui";

export function MicButton({ disabled, onTranscript, onActive, where }: {
  disabled: boolean; onTranscript: (text: string, fellBack: boolean) => void; onActive: (active: boolean) => void; where: string;
}) {
  const [state, setState] = useState<VoiceState>({ phase: "idle", seconds: 0 });
  const [mime, setMime] = useState<string>();
  const [language, setLanguage] = useState<"auto" | "en" | "hi">("auto");
  const languageRef = useRef(language);
  languageRef.current = language;
  const callbacks = useRef({ onTranscript, onActive });
  callbacks.current = { onTranscript, onActive };
  const recording = useRef<VoiceRecording | null>(null);
  useEffect(() => {
    if (typeof MediaRecorder !== "undefined" && typeof navigator.mediaDevices?.getUserMedia === "function") {
      setMime(recordingType((type) => MediaRecorder.isTypeSupported(type)));
    }
    const controller = new VoiceRecording(
      (next) => {
        setState(next);
        callbacks.current.onActive(["requesting", "recording", "transcribing"].includes(next.phase));
      },
      (text, fellBack) => callbacks.current.onTranscript(text, fellBack), (audio, signal) => transcribe(audio, signal, languageRef.current === "auto" ? undefined : languageRef.current),
    );
    recording.current = controller;
    return () => { controller.cancel(); };
  }, []);
  const recordingNow = state.phase === "recording";
  const waiting = state.phase === "requesting" || state.phase === "transcribing";
  const label = state.denied ? state.error : !mime ? "Voice recording isn't supported in this browser. You can still type."
    : recordingNow ? "Stop recording" : waiting ? (state.phase === "requesting" ? "Waiting for microphone permission" : "Transcribing recording") : "Start voice recording";

  return (
    <div className="contents">
      <div className="flex flex-col items-center gap-1">
      <select aria-label="Voice language" value={language} disabled={waiting || recordingNow}
        onChange={e => setLanguage(e.target.value as typeof language)} className="min-w-0 max-w-24 rounded border border-line bg-surface px-1 text-xs text-ink">
        <option value="auto">Auto voice</option><option value="en">English</option><option value="hi">Hindi</option>
      </select>
      <span title={label}>
        <Button type="button" aria-label={label} aria-pressed={recordingNow}
          aria-describedby="voice-status" disabled={!mime || state.denied || waiting || (disabled && !recordingNow)}
          className="h-[44px] min-w-[44px] shrink-0 px-2"
          onClick={() => recordingNow ? recording.current?.stop() : mime && void recording.current?.start(mime)}>
          {recordingNow ? <span aria-hidden="true" className="h-3 w-3 rounded-full bg-red-600" /> : (
            <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
              <rect x="9" y="2" width="6" height="12" rx="3" /><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8" />
            </svg>
          )}
          {recordingNow && <span className="num text-xs" aria-hidden="true">{state.seconds}s</span>}
        </Button>
      </span>
      </div>
      <p id="voice-status" role="status" aria-live="polite" className="sr-only">
        {state.error || (recordingNow ? "Recording. Stops automatically after 30 seconds." : waiting ? label : `Voice ready. ${where}`)}
      </p>
      {(waiting || state.error || recordingNow) && <p className="order-last col-span-3 text-xs text-muted [overflow-wrap:anywhere]">
        {state.error || (recordingNow ? "Recording — press stop when done (30s max)." : label)}
        {waiting && <Button type="button" variant="plain" className="ml-2 min-h-[44px] text-xs" onClick={() => {
          recording.current?.cancel();
          setState({ phase: "idle", seconds: 0 });
          callbacks.current.onActive(false);
        }}>Cancel voice</Button>}
      </p>}
    </div>
  );
}
