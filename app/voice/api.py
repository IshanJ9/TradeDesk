"""Raw audio in, editable text out. No chat, card or approval side effects."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.schemas import AuditKind
from app.voice.service import CONTENT_TYPES, MAX_AUDIO_BYTES, Transcriber, Transcript, VoiceError

router = APIRouter(prefix="/api/voice", tags=["voice"])


async def get_transcriber(request: Request) -> Transcriber:
    # No await between lookup and creation: all requests on this app share a limiter.
    if not hasattr(request.app.state, "voice_transcriber"):
        request.app.state.voice_transcriber = Transcriber(request.app.state.settings)
    return request.app.state.voice_transcriber


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
async def transcribe(request: Request, service: Annotated[Transcriber, Depends(get_transcriber)]) -> Transcript:
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
        result = await service.transcribe(bytes(audio), content_type)
    except VoiceError as exc:
        raise HTTPException(exc.status, exc.message) from None
    # This records metadata only. Even a transcript asking to approve an order
    # remains text returned to the editor; it is never submitted to chat here.
    request.app.state.audit.record(
        AuditKind.VOICE_TRANSCRIBED,
        "system",
        "voice transcribed" if result.seconds is None else f"voice transcribed, {result.seconds:.1f} s",
        data={"seconds": result.seconds},
    )
    return result
