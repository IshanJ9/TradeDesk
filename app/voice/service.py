"""Bounded, injectable transcription with no audio or text persistence.

Two providers: Groq (default) and, with VOICE_PROVIDER=local, faster-whisper running on this machine. The local
model is loaded only from LOCAL_VOICE_DIR (filled once by scripts/download_voice_model.py); nothing is ever
downloaded while the app runs. If the local model fails and VOICE_FALLBACK=groq, Groq transcribes instead and
the transcript says so, because the audio then left the machine.
"""

import asyncio
import io
import math
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Literal, Protocol

import httpx
from pydantic import Field

from app.config import Settings
from app.schemas import Model

MAX_AUDIO_BYTES = 5 * 1024 * 1024
CONTENT_TYPES = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "mp4",
    "audio/wav": "wav",
}
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
PROMPT = (
    "NSE, NIFTY, Sensex, Infosys, TCS, ITC, HDFC Bank, Reliance, Tata Motors, "
    "Zomato, stop-loss, intraday, delivery, limit, market, shares, rupees."
)
UNAVAILABLE = "Voice is not set up on this server"
RATE_LIMITED = "Too many voice requests, wait a moment or type instead"
FAILED = "Couldn't transcribe that, please type it"
LOCAL_UNAVAILABLE = "Local voice isn't set up on this server"
LOCAL_BUSY = "Local voice is busy with another recording"
LOCAL_TIMEOUT_SECONDS = 20.0

Provider = Literal["groq", "local"]


class Transcript(Model):
    text: str = Field(min_length=1, max_length=500)
    seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    provider: Provider = "groq"
    fell_back: bool = False  # local was chosen but failed, so Groq transcribed it (the audio was uploaded)


class VoiceStatus(Model):
    provider: Provider
    fallback: bool  # local failures go to Groq
    local_ready: bool  # the local model files and the optional package are present
    groq_ready: bool


class VoiceError(Exception):
    """Contains only a safe public message, never an upstream response."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class VoiceLimiter:
    """Sliding window shared by requests to one app instance, not per user.

    Reserve before the network await, including failed provider attempts. Multiple
    server processes each have their own limit; this is for the single demo server.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._attempts: deque[float] = deque()
        self._lock = Lock()

    def reserve(self) -> None:
        with self._lock:
            now = self._clock()
            while self._attempts and self._attempts[0] <= now - 60:
                self._attempts.popleft()
            if len(self._attempts) >= 15:
                raise VoiceError(429, RATE_LIMITED)
            self._attempts.append(now)


class LocalEngine(Protocol):
    def ready(self) -> bool: ...
    def transcribe(self, audio: bytes, language: str | None) -> tuple[str, float | None]: ...


class WhisperEngine:
    """faster-whisper on the CPU, loaded once from a local folder. Never downloads a model."""

    def __init__(self, model_dir: str):
        self._dir = Path(model_dir)
        self._model = None
        self._load_lock = Lock()

    def ready(self) -> bool:
        try:
            import faster_whisper  # noqa: F401  (optional dependency: requirements-voice-local.txt)
        except ImportError:
            return False
        return (self._dir / "model.bin").is_file()

    def transcribe(self, audio: bytes, language: str | None) -> tuple[str, float | None]:
        if not self.ready():
            raise VoiceError(503, LOCAL_UNAVAILABLE)
        with self._load_lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(str(self._dir), device="cpu", compute_type="int8", local_files_only=True)
        segments, info = self._model.transcribe(
            _decode(audio), language=language, initial_prompt=PROMPT, temperature=0.0, beam_size=5,
            condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        return text, getattr(info, "duration", None)


SAMPLE_RATE = 16_000
MAX_SECONDS = 35  # the browser stops at 30 s; anything longer is refused rather than processed


def _decode(audio: bytes):
    """Any of the accepted containers -> 16 kHz mono float32 samples. Decoded here (not by faster-whisper) so a
    PyAV upgrade can't break it, and so the work is bounded by length."""
    import av
    import numpy as np

    chunks, total = [], 0
    with av.open(io.BytesIO(audio), mode="r") as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                chunk = out.to_ndarray().reshape(-1)
                total += chunk.size
                if total > SAMPLE_RATE * MAX_SECONDS:
                    raise VoiceError(413, "The recording is too long")
                chunks.append(chunk)
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        raise VoiceError(400, "The recording is empty")
    return np.concatenate(chunks).astype(np.float32) / 32768.0


class Transcriber:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        limiter: VoiceLimiter | None = None,
        engine: LocalEngine | None = None,
    ):
        self._key = settings.groq_api_key
        self._model = settings.voice_model
        self._client = client  # injected clients are owned/closed by their caller
        self._limiter = limiter or VoiceLimiter()
        self._provider: Provider = "local" if settings.voice_provider == "local" else "groq"
        self._fallback = settings.voice_fallback == "groq"
        self._engine: LocalEngine | None = engine or (WhisperEngine(settings.local_voice_dir) if self._provider == "local" else None)
        self._local_busy = asyncio.Lock()

    def status(self) -> VoiceStatus:
        return VoiceStatus(
            provider=self._provider, fallback=self._provider == "local" and self._fallback and bool(self._key),
            local_ready=bool(self._engine and self._engine.ready()), groq_ready=bool(self._key),
        )

    async def transcribe(self, audio: bytes, content_type: str, *, language: str | None = None) -> Transcript:
        if language not in (None, 'en', 'hi'):
            raise VoiceError(422, "Choose English, Hindi or automatic language detection")
        if content_type not in CONTENT_TYPES:
            raise VoiceError(415, "Use audio/webm, audio/ogg, audio/mp4 or audio/wav")
        if not audio:
            raise VoiceError(400, "The recording is empty")
        if len(audio) > MAX_AUDIO_BYTES:
            raise VoiceError(413, "The recording must be 5 MB or smaller")
        if self._provider == "groq" and not self._key:
            raise VoiceError(503, UNAVAILABLE)
        self._limiter.reserve()

        if self._provider == "local":
            try:
                return await self._local(audio, language)
            except VoiceError:
                if not (self._fallback and self._key):
                    raise
            # the local model failed: Groq transcribes it instead, and the transcript says so
            return (await self._groq(audio, content_type, language)).model_copy(update={"fell_back": True})
        return await self._groq(audio, content_type, language)

    async def _local(self, audio: bytes, language: str | None) -> Transcript:
        engine = self._engine
        if engine is None or not engine.ready():
            raise VoiceError(503, LOCAL_UNAVAILABLE)
        if self._local_busy.locked():  # one recording at a time on the CPU
            raise VoiceError(429, LOCAL_BUSY)
        await self._local_busy.acquire()
        work = asyncio.ensure_future(asyncio.to_thread(engine.transcribe, audio, language))
        # the lock is held until the thread really finishes, even if we stop waiting for it
        work.add_done_callback(lambda _f: self._local_busy.release())
        try:
            text, duration = await asyncio.wait_for(asyncio.shield(work), LOCAL_TIMEOUT_SECONDS)
        except VoiceError:
            raise
        except Exception:  # timeout, decode error, model error: never the details
            raise VoiceError(502, FAILED) from None
        text = text.strip()
        if not text:
            raise VoiceError(502, FAILED)
        seconds = float(duration) if type(duration) in (int, float) and math.isfinite(duration) and duration >= 0 else None
        return Transcript(text=text[:500].strip(), seconds=seconds, provider="local")

    async def _groq(self, audio: bytes, content_type: str, language: str | None) -> Transcript:
        try:
            if self._client is not None:
                response = await self._post(self._client, audio, content_type, language)
            else:
                # No retries, redirects, or environment proxy credentials. Close the
                # client per request so this router needs no extra lifespan hooks.
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await self._post(client, audio, content_type, language)
            if response.status_code == 429:
                raise VoiceError(429, RATE_LIMITED)
            if response.status_code != 200:
                raise VoiceError(502, FAILED)
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
                raise VoiceError(502, FAILED)
            text = payload["text"].strip()
            # Fail closed if an upstream service ever reflects our credential.
            if not text or self._key in text:
                raise VoiceError(502, FAILED)
            duration = payload.get("duration")
            seconds = (
                float(duration)
                if type(duration) in (int, float) and math.isfinite(duration) and duration >= 0
                else None
            )
            return Transcript(text=text[:500].strip(), seconds=seconds)
        except (httpx.HTTPError, ValueError, OverflowError):
            raise VoiceError(502, FAILED) from None

    async def _post(self, client: httpx.AsyncClient, audio: bytes, content_type: str, language: str | None = None) -> httpx.Response:
        return await client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {self._key}"},
            files={"file": (f"recording.{CONTENT_TYPES[content_type]}", audio, content_type)},
            data={"model": self._model, "temperature": "0", "response_format": "json", "prompt": PROMPT,
                  **({'language':language} if language else {})},
            timeout=20.0,
            follow_redirects=False,
        )
