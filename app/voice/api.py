"""Raw audio in, editable text out. No chat, card or approval side effects."""

from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request

from app.desk import desk_router
from app.schemas import AuditKind
from app.voice.service import CONTENT_TYPES, MAX_AUDIO_BYTES, Transcriber, Transcript, VoiceError, VoiceStatus

router = desk_router(prefix="/api/voice", tags=["voice"])


async def get_transcriber(request: Request) -> Transcriber:
    # No await between lookup and creation: all requests on this app share a limiter.
    if not hasattr(request.app.state, "voice_transcriber"):
        request.app.state.voice_transcriber = Transcriber(request.app.state.settings)
    return request.app.state.voice_transcriber


@router.get("/status", response_model=VoiceStatus)
async def status(service: Annotated[Transcriber, Depends(get_transcriber)]) -> VoiceStatus:
    """Which provider transcribes, so the microphone can say where the audio goes. Loads nothing."""
    return service.status()


@router.post(
    "/transcribe",
    response_model=Transcript,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {mime: {"schema": {"type": "string", "format": "binary"}} for mime in CONTENT_TYPES},
        }
    },
)
async def transcribe(request: Request, service: Annotated[Transcriber, Depends(get_transcriber)],
                     language: Literal['en','hi'] | None = None) -> Transcript:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in CONTENT_TYPES:
        raise HTTPException(415, "Use audio/webm, audio/ogg, audio/mp4 or audio/wav")
    # Check the actual stream too: Content-Length may be absent or inaccurate.
    length = request.headers.get("content-length")
    if length is not None:
        try:
            size = int(length)
        except ValueError:
            raise HTTPException(400, "Invalid recording size") from None
        if size < 0:
            raise HTTPException(400, "Invalid recording size")
        if size > MAX_AUDIO_BYTES:
            raise HTTPException(413, "The recording must be 5 MB or smaller")
    audio = bytearray()
    async for chunk in request.stream():
        if len(audio) + len(chunk) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "The recording must be 5 MB or smaller")
        audio.extend(chunk)
    try:
        result = await service.transcribe(bytes(audio), content_type, language=language) if language else await service.transcribe(bytes(audio), content_type)
    except VoiceError as exc:
        raise HTTPException(exc.status, exc.message) from None
    # This records metadata only. Even a transcript asking to approve an order
    # remains text returned to the editor; it is never submitted to chat here.
    where = "on this machine" if result.provider == "local" else "by Groq after the local model failed" if result.fell_back else ""
    request.state.ws.audit.record(
        AuditKind.VOICE_TRANSCRIBED,
        "system",
        " ".join(filter(None, ["voice transcribed", where])) + ("" if result.seconds is None else f", {result.seconds:.1f} s"),
        data={"seconds": result.seconds, "provider": result.provider, "fell_back": result.fell_back},
    )
    return result
