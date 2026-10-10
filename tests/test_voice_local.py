"""Local voice (VOICE_PROVIDER=local): a fake engine stands in for faster-whisper, so no model, microphone or
network is needed. Groq is a MockTransport that records whether the audio was ever uploaded."""

import asyncio
import threading
import time

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.voice import service
from app.voice.service import FAILED, LOCAL_BUSY, LOCAL_UNAVAILABLE, Transcriber, VoiceLimiter, WhisperEngine

AUDIO = b"test-audio-never-persist-this"
PATH = "/api/voice/transcribe"


class FakeEngine:
    def __init__(self, text="buy 5 ITC at market", *, ready=True, fail=False, delay=0.0, gate=None):
        self.text, self._ready, self.fail, self.delay, self.gate = text, ready, fail, delay, gate
        self.calls: list[str | None] = []

    def ready(self):
        return self._ready

    def transcribe(self, audio, language):
        self.calls.append(language)
        if self.gate:
            self.gate.wait(5)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("decoder exploded with internal details")
        return self.text, 2.5


@pytest.fixture
async def make_local():
    resources = []

    def make(engine, *, key="fake-groq-key", fallback="groq"):
        uploads = []

        async def groq(request):
            uploads.append(request)
            return httpx.Response(200, json={"text": "from groq", "duration": 3.0})

        settings = Settings(broker="mock", llm_provider="rules", ticker_interval=None, reconcile_interval=None,
                            groq_api_key=key, voice_provider="local", voice_fallback=fallback)
        app = create_app(settings)
        mock = httpx.AsyncClient(transport=httpx.MockTransport(groq))
        app.state.voice_transcriber = Transcriber(settings, client=mock, limiter=VoiceLimiter(lambda: 0.0), engine=engine)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        resources.append((client, mock, app.state.db))
        return client, app, uploads

    yield make
    for client, mock, db in resources:
        await client.aclose()
        await mock.aclose()
        db.close()


async def upload(client, language=None):
    return await client.post(PATH + (f"?language={language}" if language else ""), content=AUDIO,
                             headers={"Content-Type": "audio/webm"})


async def test_local_transcription_never_uploads_the_audio(make_local):
    engine = FakeEngine()
    client, app, uploads = make_local(engine)
    response = await upload(client, "hi")
    assert response.status_code == 200
    assert response.json() == {"text": "buy 5 ITC at market", "seconds": 2.5, "provider": "local", "fell_back": False}
    assert uploads == []
    assert engine.calls == ["hi"]  # the language hint reaches the local model
    event, = app.state.audit.list()
    assert event.summary == "voice transcribed on this machine, 2.5 s"
    assert event.data == {"seconds": 2.5, "provider": "local", "fell_back": False}
    assert "buy 5 ITC" not in app.state.audit.export_jsonl()  # metadata only, never the words


@pytest.mark.parametrize("engine", [FakeEngine(ready=False), FakeEngine(fail=True), FakeEngine(text="   ")])
async def test_a_local_failure_falls_back_to_groq_and_says_so(make_local, engine):
    client, app, uploads = make_local(engine)
    response = await upload(client)
    assert response.status_code == 200
    assert response.json() == {"text": "from groq", "seconds": 3.0, "provider": "groq", "fell_back": True}
    assert len(uploads) == 1
    event, = app.state.audit.list()
    assert "by Groq after the local model failed" in event.summary


@pytest.mark.parametrize("engine, status, message", [
    (FakeEngine(ready=False), 503, LOCAL_UNAVAILABLE),
    (FakeEngine(fail=True), 502, FAILED),
])
async def test_without_fallback_the_audio_stays_local(make_local, engine, status, message):
    client, app, uploads = make_local(engine, fallback="none")
    response = await upload(client)
    assert (response.status_code, response.json()) == (status, {"detail": message})
    assert uploads == []
    assert "exploded" not in response.text  # never the internal error


async def test_no_groq_key_means_no_fallback(make_local):
    client, _app, uploads = make_local(FakeEngine(ready=False), key="")
    response = await upload(client)
    assert (response.status_code, response.json()) == (503, {"detail": LOCAL_UNAVAILABLE})
    assert uploads == []


async def test_one_recording_at_a_time_on_the_cpu(make_local):
    gate = threading.Event()
    engine = FakeEngine(gate=gate)
    client, _app, uploads = make_local(engine, fallback="none")
    first = asyncio.create_task(upload(client))
    for _ in range(100):  # wait until the first recording is inside the model
        if engine.calls:
            break
        await asyncio.sleep(0.01)
    second = await upload(client)
    assert (second.status_code, second.json()) == (429, {"detail": LOCAL_BUSY})
    gate.set()
    assert (await first).status_code == 200
    assert uploads == []
    assert (await upload(client)).status_code == 200  # free again once the first finished


async def test_a_slow_local_model_times_out(make_local, monkeypatch):
    monkeypatch.setattr(service, "LOCAL_TIMEOUT_SECONDS", 0.05)
    client, _app, uploads = make_local(FakeEngine(delay=0.5), fallback="none")
    response = await upload(client)
    assert (response.status_code, response.json()) == (502, {"detail": FAILED})
    assert uploads == []


async def test_status_says_where_the_audio_goes(make_local):
    client, _app, _uploads = make_local(FakeEngine(ready=False))
    assert (await client.get("/api/voice/status")).json() == {
        "provider": "local", "fallback": True, "local_ready": False, "groq_ready": True}
    client2, _app2, _ = make_local(FakeEngine(), key="")
    assert (await client2.get("/api/voice/status")).json() == {
        "provider": "local", "fallback": False, "local_ready": True, "groq_ready": False}


async def test_groq_is_still_the_default(make_local):
    settings = Settings(broker="mock", llm_provider="rules", ticker_interval=None, reconcile_interval=None, groq_api_key="k")
    assert Transcriber(settings).status().provider == "groq"


def test_the_real_engine_never_downloads_a_model(tmp_path):
    engine = WhisperEngine(str(tmp_path / "missing"))
    assert engine.ready() is False
    with pytest.raises(service.VoiceError) as caught:
        engine.transcribe(AUDIO, None)
    assert caught.value.status == 503
    assert not (tmp_path / "missing").exists()  # nothing was fetched or created
