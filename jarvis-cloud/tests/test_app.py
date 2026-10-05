import asyncio
import io
import json
import wave
import xml.etree.ElementTree as ET
import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from main import app
from providers import Providers, ssml, wav_bytes
from tool_registry import execute

ORIGIN = "http://localhost:8000"
KEY = "test-only-" + "a" * 40


class FakeProviders:
    def __init__(self):
        self.calls = 0
        self.cancelled = False
        self.hang = False

    async def transcribe(self, pcm, rate, language):
        assert len(pcm) == 32000 and rate == 16000
        self.calls += 1
        return "வணக்கம், explain Python."

    async def reply(self, messages):
        self.calls += 1
        if self.hang:
            try:
                await asyncio.sleep(60)
            finally:
                self.cancelled = True
        yield "வணக்கம். "
        yield "Hello."
        messages.append({"role": "assistant", "content": "வணக்கம். Hello."})

    async def speak(self, text):
        self.calls += 1
        yield b"\x00\x01" * 100


@pytest.fixture
def client(monkeypatch):
    for key, value in {"APP_ACCESS_KEY": KEY, "GROQ_API_KEY": "fake-groq",
                       "AZURE_SPEECH_KEY": "fake-azure", "AZURE_SPEECH_REGION": "centralindia",
                       "ALLOWED_ORIGINS": ORIGIN}.items():
        monkeypatch.setenv(key, value)
    with TestClient(app) as client:
        app.state.providers = FakeProviders()
        yield client


def connect(client, key=KEY):
    return client.websocket_connect("/ws", headers={"origin": ORIGIN})


def authorize(ws):
    ws.send_json({"type": "auth", "key": KEY})
    assert ws.receive_json()["type"] == "ready"


def collect(ws):
    events, chunks = [], []
    for _ in range(30):
        event = ws.receive()
        if event.get("bytes") is not None:
            chunks.append(event["bytes"])
        else:
            event = json.loads(event["text"])
            events.append(event)
            if event["type"] in {"done", "error", "cancelled"}:
                return events, chunks
    pytest.fail("Turn did not finish")


def test_static_and_health(client):
    response = client.get("/")
    assert response.status_code == 200 and "JARVIS" in response.text
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert client.get("/.env").status_code == 404
    assert client.get("/healthz").json() == {"status": "ok"}


def test_wrong_key_cannot_reach_providers(client):
    with connect(client) as ws:
        ws.send_json({"type": "auth", "key": "wrong"})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1008
    assert app.state.providers.calls == 0


def test_wrong_origin(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"origin": "https://attacker.example"}):
            pass


def test_audio_turn(client):
    with connect(client) as ws:
        authorize(ws)
        ws.send_json({"type": "start", "sample_rate": 16000, "language": "auto"})
        assert ws.receive_json()["type"] == "recording"
        for _ in range(8):
            ws.send_bytes(b"\x00\x01" * 2000)
        ws.send_json({"type": "stop"})
        events, chunks = collect(ws)
        assert any(e.get("text", "").startswith("வணக்கம்") for e in events)
        assert events[-1]["type"] == "done" and b"".join(chunks)


def test_short_audio_and_invalid_frame(client):
    with connect(client) as ws:
        authorize(ws)
        ws.send_json({"type": "start", "sample_rate": 16000})
        ws.receive_json()
        ws.send_bytes(b"\0\0" * 10)
        ws.send_json({"type": "stop"})
        assert ws.receive_json()["type"] == "error"
        assert app.state.providers.calls == 0
        ws.send_bytes(b"\0\0")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_only_one_authenticated_session(client):
    with connect(client) as first:
        authorize(first)
        with connect(client) as second:
            second.send_json({"type": "auth", "key": KEY})
            with pytest.raises(WebSocketDisconnect) as exc:
                second.receive_json()
            assert exc.value.code == 1013


def test_cancel_interrupts_pipeline_and_allows_next_turn(client):
    provider = app.state.providers
    provider.hang = True
    with connect(client) as ws:
        authorize(ws)
        ws.send_json({"type": "text", "text": "hello"})
        while ws.receive_json()["type"] != "audio_format":
            pass
        ws.send_json({"type": "cancel"})
        assert collect(ws)[0][-1]["type"] == "cancelled"
        assert provider.cancelled
        provider.hang = False
        ws.send_json({"type": "text", "text": "try again"})
        assert collect(ws)[0][-1]["type"] == "done"


def test_provider_quota_error_is_sanitized(client):
    async def broken(text):
        response = httpx.Response(429, request=httpx.Request("POST", "https://provider.example"))
        raise httpx.HTTPStatusError("SECRET CREDENTIAL", request=response.request, response=response)
        yield b""  # Make this an async generator.
    app.state.providers.speak = broken
    with connect(client) as ws:
        authorize(ws)
        ws.send_json({"type": "text", "text": "hello"})
        events, _ = collect(ws)
        assert "quota" in events[-1]["message"]
        assert "SECRET" not in json.dumps(events)


def test_audio_buffer_limit(client):
    with connect(client) as ws:
        authorize(ws)
        ws.send_json({"type": "start", "sample_rate": 16000})
        ws.receive_json()
        for _ in range(41):
            ws.send_bytes(b"\0\0" * 8000)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert app.state.providers.calls == 0


def test_wav_and_ssml_are_valid(client):
    with wave.open(io.BytesIO(wav_bytes(b"\0\0" * 16000, 16000))) as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getnframes()) == (1, 2, 16000)
    xml = ssml('வணக்கம் Python & <tools> "hello"', app.state.settings)
    ET.fromstring(xml)
    assert "ta-IN-PallaviNeural" in xml and "en-IN-NeerjaNeural" in xml
    assert "&lt;tools&gt;" in xml and "&amp;" in xml


def test_tool_registry_is_allowlisted_and_validates():
    assert "Unknown" in asyncio.run(execute("run_shell", '{}'))
    assert "Invalid" in asyncio.run(execute("get_server_time", '{"shell":"rm"}'))
    assert "utc" in json.loads(asyncio.run(execute("get_server_time", '{}')))


def test_real_adapters_with_mock_http(client):
    async def run():
        requests = []
        def handler(request):
            requests.append(request)
            if "transcriptions" in str(request.url):
                assert b"RIFF" in request.content
                return httpx.Response(200, json={"text": "வணக்கம்"})
            if "completions" in str(request.url):
                payload = json.loads(request.content)
                assert payload["stream"] is True
                event = {"choices": [{"delta": {"content": "Hello."}}]}
                return httpx.Response(200, text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")
            assert request.headers["x-microsoft-outputformat"] == "raw-24khz-16bit-mono-pcm"
            ET.fromstring(request.content)
            return httpx.Response(200, content=b"\0\1" * 3000)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            provider = Providers(app.state.settings, http)
            assert await provider.transcribe(b"\0\0" * 16000, 16000, "auto") == "வணக்கம்"
            messages = [{"role": "user", "content": "hello"}]
            assert "".join([x async for x in provider.reply(messages)]) == "Hello."
            assert len(b"".join([x async for x in provider.speak("Hello.")])) == 6000
            assert len(requests) == 3
    asyncio.run(run())


def test_fragmented_tool_call_completes_before_answer(client):
    async def run():
        count = 0
        def handler(request):
            nonlocal count
            count += 1
            payload = json.loads(request.content)
            if count == 1:
                deltas = [
                    {"tool_calls": [{"index": 0, "id": "call-1", "function": {
                        "name": "get_server_time", "arguments": "{"}}]},
                    {"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]},
                ]
            else:
                assert payload["messages"][-1]["role"] == "tool"
                assert "utc" in json.loads(payload["messages"][-1]["content"])
                assert payload["messages"][-1]["tool_call_id"] == "call-1"
                deltas = [{"content": "The server time is available."}]
            stream = "".join("data: " + json.dumps({"choices": [{"delta": d}]}) + "\n\n"
                             for d in deltas) + "data: [DONE]\n\n"
            return httpx.Response(200, text=stream)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            provider = Providers(app.state.settings, http)
            messages = [{"role": "user", "content": "What time is it?"}]
            result = "".join([part async for part in provider.reply(messages)])
            assert result == "The server time is available." and count == 2
    asyncio.run(run())
