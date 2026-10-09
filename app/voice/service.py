"""Bounded, injectable Groq transcription with no audio or text persistence."""

import math
import time
from collections import deque
from collections.abc import Callable
from threading import Lock

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


class Transcript(Model):
    text: str = Field(min_length=1, max_length=500)
    seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)


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


class Transcriber:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        limiter: VoiceLimiter | None = None,
    ):
        self._key = settings.groq_api_key
        self._model = settings.voice_model
        self._client = client  # injected clients are owned/closed by their caller
        self._limiter = limiter or VoiceLimiter()

    async def transcribe(self, audio: bytes, content_type: str) -> Transcript:
        if content_type not in CONTENT_TYPES:
            raise VoiceError(415, "Use audio/webm, audio/ogg, audio/mp4 or audio/wav")
        if not audio:
            raise VoiceError(400, "The recording is empty")
        if len(audio) > MAX_AUDIO_BYTES:
            raise VoiceError(413, "The recording must be 5 MB or smaller")
        if not self._key:
            raise VoiceError(503, UNAVAILABLE)
        self._limiter.reserve()

        try:
            if self._client is not None:
                response = await self._post(self._client, audio, content_type)
            else:
                # No retries, redirects, or environment proxy credentials. Close the
                # client per request so this router needs no extra lifespan hooks.
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await self._post(client, audio, content_type)
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

    async def _post(self, client: httpx.AsyncClient, audio: bytes, content_type: str) -> httpx.Response:
        return await client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {self._key}"},
            files={"file": (f"recording.{CONTENT_TYPES[content_type]}", audio, content_type)},
            data={"model": self._model, "temperature": "0", "response_format": "json", "prompt": PROMPT},
            timeout=20.0,
            follow_redirects=False,
        )
